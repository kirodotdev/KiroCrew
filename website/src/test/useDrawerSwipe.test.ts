import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { animate, motionValue } from 'framer-motion'
import { useDrawerSwipe } from '../hooks/useDrawerSwipe'

/** Events carry an explicit timeStamp: velocity is a real branch of the
 *  release decision, and jsdom stamps events created in a loop within the same
 *  millisecond, which would pin every gesture's velocity at 0. */
function touch(type: string, clientX: number, clientY = 0, timeStamp = 0): TouchEvent {
  const t = { clientX, clientY } as Touch
  const init: TouchEventInit = { bubbles: true }
  if (type === 'touchstart' || type === 'touchmove') init.touches = [t]
  if (type === 'touchend' || type === 'touchcancel') init.changedTouches = [t]
  const e = new TouchEvent(type, init)
  Object.defineProperty(e, 'timeStamp', { value: timeStamp })
  return e
}

describe('useDrawerSwipe', () => {
  let el: HTMLDivElement
  let ref: { current: HTMLDivElement }
  let x: ReturnType<typeof motionValue<number>>
  let onGestureOpen: ReturnType<typeof vi.fn>
  let onSettle: ReturnType<typeof vi.fn>

  /** Viewport width doubles as the gesture's full travel, so closed is -400. */
  const CLOSED = -400

  beforeEach(() => {
    el = document.createElement('div')
    document.body.appendChild(el)
    ref = { current: el }
    x = motionValue(0)
    onGestureOpen = vi.fn()
    onSettle = vi.fn()
    Object.defineProperty(window, 'innerWidth', { writable: true, value: 400 })
  })

  function mount(open = false) {
    return renderHook(() => useDrawerSwipe(ref, {
      enabled: true, open, x, onGestureOpen, onSettle,
    }))
  }

  /** Dispatch inside act(): the axis lock flips React state mid-gesture. */
  function fire(target: EventTarget, e: TouchEvent) {
    act(() => { target.dispatchEvent(e) })
  }

  // ── The behaviour the predecessor could not express ──────────────────────
  // useSwipeEdge read displacement once on touchend, so nothing tracked the
  // finger and a reconsidered drag still committed. These three are the point
  // of the rewrite.

  it('moves the panel with the finger instead of waiting for release', () => {
    mount()
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 60))   // past AXIS_LOCK -> locks, mounts
    expect(x.get()).toBe(CLOSED + 20)
    fire(el, touch('touchmove', 240))
    expect(x.get()).toBe(CLOSED + 200)
  })

  it('mounts the panel at the axis lock, not at touchstart', () => {
    mount()
    fire(el, touch('touchstart', 40))
    expect(onGestureOpen).not.toHaveBeenCalled()
    fire(el, touch('touchmove', 44))   // below AXIS_LOCK — still undecided
    expect(onGestureOpen).not.toHaveBeenCalled()
    fire(el, touch('touchmove', 60))
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
  })

  it('cancels when the finger comes back, however far out it went', async () => {
    mount()
    fire(el, touch('touchstart', 40, 0, 0))
    fire(el, touch('touchmove', 300, 0, 100))   // most of the way open
    fire(el, touch('touchmove', 45, 0, 400))    // ...and back again, slowly
    fire(el, touch('touchend', 45, 0, 500))
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(false))
  })

  it('keeps the base it locked with when the mount flips `open` mid-gesture', () => {
    // The real sequence, and the one jsdom will not produce on its own: the
    // opening drag mounts the panel from inside the touchmove handler, the
    // browser flushes that synchronously, and `open` is already true when the
    // same handler reaches the tracking line. Re-reading it there recomputed the
    // base as 0 (an already-open panel), the offset clamped to 0, and the panel
    // appeared at rest — the snap this hook exists to remove. Only the browser
    // harness caught it, so this pins the invariant here too.
    const { rerender } = renderHook(
      ({ open }: { open: boolean }) => useDrawerSwipe(ref, {
        enabled: true, open, x, onGestureOpen, onSettle,
      }),
      { initialProps: { open: false } },
    )
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 60))
    expect(onGestureOpen).toHaveBeenCalledTimes(1)

    // What the synchronous mount does to the hook's view of the world.
    rerender({ open: true })

    fire(el, touch('touchmove', 190))
    expect(x.get()).toBe(CLOSED + 150)   // NOT 0
  })

  // ── Release decision ────────────────────────────────────────────────────

  it('commits open past the halfway point', async () => {
    mount()
    fire(el, touch('touchstart', 40, 0, 0))
    fire(el, touch('touchmove', 300, 0, 200))
    fire(el, touch('touchend', 300, 0, 400))    // stale sample -> no flick
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(true))
  })

  it('a flick commits open from well short of halfway', async () => {
    mount()
    fire(el, touch('touchstart', 40, 0, 0))
    fire(el, touch('touchmove', 70, 0, 10))
    fire(el, touch('touchmove', 110, 0, 20))    // 4 px/ms, far above COMMIT_VELOCITY
    fire(el, touch('touchend', 110, 0, 25))
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(true))
  })

  it('a hold at the same spot does not inherit the flick that got it there', async () => {
    mount()
    fire(el, touch('touchstart', 40, 0, 0))
    fire(el, touch('touchmove', 110, 0, 20))    // fast...
    fire(el, touch('touchend', 110, 0, 300))    // ...then held for 280ms
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(false))
  })

  it('closes an open panel on a leftward drag past halfway', async () => {
    x.set(0)
    mount(true)
    fire(el, touch('touchstart', 380, 0, 0))
    fire(el, touch('touchmove', 100, 0, 200))
    expect(x.get()).toBe(-280)
    fire(el, touch('touchend', 100, 0, 400))
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(false))
  })

  // ── What the gesture must NOT claim ─────────────────────────────────────

  it('leaves the platform back-swipe band at the bezel alone', () => {
    mount()
    fire(el, touch('touchstart', 8))   // inside the OS gesture's own strip
    fire(el, touch('touchmove', 200))
    expect(onGestureOpen).not.toHaveBeenCalled()
    expect(x.get()).toBe(0)
  })

  it('is a band, not the left third of the screen', () => {
    mount()
    // 137px was inside the predecessor's 35%-of-viewport zone, so a rightward
    // drag begun mid-message opened the drawer.
    fire(el, touch('touchstart', 137))
    fire(el, touch('touchmove', 300))
    expect(onGestureOpen).not.toHaveBeenCalled()
  })

  it('yields to a vertical scroll', () => {
    mount()
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 50, 40))   // dy 40 > dx 10
    fire(el, touch('touchmove', 200, 40))  // abandoned — cannot be reclaimed
    expect(onGestureOpen).not.toHaveBeenCalled()
  })

  it('ignores a leftward drag while closed and a rightward one while open', () => {
    const closed = mount()
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 10))
    expect(onGestureOpen).not.toHaveBeenCalled()
    closed.unmount()

    x.set(0)
    mount(true)
    fire(el, touch('touchstart', 200))
    fire(el, touch('touchmove', 320))
    expect(x.get()).toBe(0)
  })

  // ── Giving up a gesture that already owns the panel ─────────────────────
  // A cancelled gesture is not a released one: the release handler never runs,
  // so if abandoning only stopped tracking, the panel would be stranded
  // wherever the finger left it — mounted, half-open, scrim half-dimmed, with
  // no animation coming. Each of these asserts it goes back to where the
  // gesture STARTED.

  it('slides an interrupted opening drag back closed', async () => {
    mount()
    fire(el, touch('touchstart', 40, 0, 0))
    fire(el, touch('touchmove', 300, 0, 100))   // most of the way open
    expect(x.get()).toBe(CLOSED + 260)
    fire(el, touch('touchcancel', 300, 0, 120))
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(false))
    expect(x.get()).toBe(CLOSED)
  })

  it('slides an interrupted closing drag back open', async () => {
    x.set(0)
    mount(true)
    fire(el, touch('touchstart', 380, 0, 0))
    fire(el, touch('touchmove', 120, 0, 100))
    expect(x.get()).toBe(-260)
    fire(el, touch('touchcancel', 120, 0, 120))
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(true))
    expect(x.get()).toBe(0)
  })

  it('treats a second finger mid-drag as an interruption, not a freeze', async () => {
    mount()
    fire(el, touch('touchstart', 40, 0, 0))
    fire(el, touch('touchmove', 200, 0, 100))
    const pinch = new TouchEvent('touchmove', {
      bubbles: true,
      touches: [{ clientX: 200, clientY: 0 } as Touch, { clientX: 250, clientY: 0 } as Touch],
    })
    Object.defineProperty(pinch, 'timeStamp', { value: 120 })
    fire(el, pinch)
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(false))
    expect(x.get()).toBe(CLOSED)
    // And the gesture is usable again rather than stuck mid-flight.
    fire(el, touch('touchstart', 40, 0, 200))
    fire(el, touch('touchmove', 60, 0, 220))
    expect(onGestureOpen).toHaveBeenCalledTimes(2)
  })

  it('gives the panel up the moment the second finger LANDS, before it moves', async () => {
    // A pinch that holds still emits no further touchmove. Waiting for one left
    // the panel owned and stranded for as long as the fingers rested, so the
    // multi-touch check runs before the phase guard in touchstart.
    mount()
    fire(el, touch('touchstart', 40, 0, 0))
    fire(el, touch('touchmove', 200, 0, 100))
    const land = new TouchEvent('touchstart', {
      bubbles: true,
      touches: [{ clientX: 200, clientY: 0 } as Touch, { clientX: 250, clientY: 0 } as Touch],
    })
    Object.defineProperty(land, 'timeStamp', { value: 110 })
    fire(el, land)
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(false))
    expect(x.get()).toBe(CLOSED)
  })

  it('a mid-gesture unbind leaves the next bind able to start a gesture', () => {
    // `phase` is a ref, so it outlives the listener teardown. Left at 'locked'
    // it made every later touchstart bail at the idle guard — the gesture was
    // dead for the rest of the mount, with one stray jump from a stale startX
    // on the way.
    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useDrawerSwipe(ref, {
        enabled, open: false, x, onGestureOpen, onSettle,
      }),
      { initialProps: { enabled: true } },
    )
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 200))        // locked, panel owned
    rerender({ enabled: false })             // e.g. crossing out of mobile
    rerender({ enabled: true })              // ...and back
    onGestureOpen.mockClear()
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 200))
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
  })

  it('takes the value over from an animation the CONSUMER started', async () => {
    // The toggle, the backdrop tap and the session-selected close all animate
    // this same value from outside the hook, and discard the stop handle.
    // `x.set()` does not cancel an animation, so a drag begun inside one of
    // those windows had the drag and the animation both writing every frame.
    // Tracking only the hook's own settles could not see this one.
    mount()
    const programmatic = animate(x, 0, { duration: 0.4 })
    expect(programmatic.time).toBeDefined()   // it is live
    fire(el, touch('touchstart', 40, 0, 0))
    fire(el, touch('touchmove', 200, 0, 40))  // locks -> must seize the value
    const seized = x.get()
    expect(seized).toBe(CLOSED + 160)
    // Let real time pass. If the animation were still running it would drag the
    // value back toward 0 behind the finger's back.
    await new Promise(r => setTimeout(r, 120))
    expect(x.get()).toBe(seized)
  })

  it('binds nothing when disabled', () => {
    renderHook(() => useDrawerSwipe(ref, {
      enabled: false, open: false, x, onGestureOpen, onSettle,
    }))
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 300))
    expect(onGestureOpen).not.toHaveBeenCalled()
  })

  it('resets on touchcancel', () => {
    mount()
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchcancel', 40))
    fire(el, touch('touchmove', 300))
    expect(onGestureOpen).not.toHaveBeenCalled()
  })

  // ── Horizontal scroller ownership (carried over from useSwipeEdge) ───────
  // A wide code block or a card strip under the finger owns the gesture while
  // it still has somewhere to scroll. Losing this makes every horizontal pan
  // inside a message close or open the drawer.

  function appendScroller(scrollLeft: number, scrollWidth = 900, clientWidth = 300): HTMLDivElement {
    const sc = document.createElement('div')
    sc.style.overflowX = 'auto'
    Object.defineProperty(sc, 'scrollWidth', { configurable: true, value: scrollWidth })
    Object.defineProperty(sc, 'clientWidth', { configurable: true, value: clientWidth })
    Object.defineProperty(sc, 'scrollLeft', { configurable: true, writable: true, value: scrollLeft })
    el.appendChild(sc)
    return sc
  }

  it('does not close over a scroller that can still reveal more', () => {
    const sc = appendScroller(0)
    x.set(0)
    mount(true)
    expect(sc.scrollWidth - sc.clientWidth).toBe(600)
    fire(sc, touch('touchstart', 200))
    fire(sc, touch('touchmove', 100))
    expect(x.get()).toBe(0)
  })

  it('does not close when the scroller consumed the gesture', () => {
    const sc = appendScroller(600)
    x.set(0)
    mount(true)
    fire(sc, touch('touchstart', 200))
    sc.scrollLeft = 540
    fire(sc, touch('touchmove', 100))
    expect(x.get()).toBe(0)
  })

  it('closes over a scroller already at its end that did not move', () => {
    const sc = appendScroller(600)
    x.set(0)
    mount(true)
    expect(sc.scrollLeft).toBe(sc.scrollWidth - sc.clientWidth)
    fire(sc, touch('touchstart', 200))
    fire(sc, touch('touchmove', 100))
    expect(x.get()).toBe(-100)
  })

  it('opens from the left band over a scroller already at its start', () => {
    const sc = appendScroller(0)
    mount()
    fire(sc, touch('touchstart', 40))
    fire(sc, touch('touchmove', 200))
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
  })
})
