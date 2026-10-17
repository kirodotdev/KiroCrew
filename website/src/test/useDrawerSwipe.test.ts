import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { animate, motionValue } from 'framer-motion'
import { useDrawerSwipe } from '../hooks/useDrawerSwipe'

/** Events carry an explicit timeStamp: velocity is a real branch of the
 *  release decision, and jsdom stamps events created in a loop within the same
 *  millisecond, which would pin every gesture's velocity at 0. */
function touch(type: string, clientX: number, clientY = 0, timeStamp = 0): TouchEvent {
  const t = { clientX, clientY } as Touch
  // `composed: true` is what a real touch event carries, and it is what lets the
  // event escape a shadow root — without it a fixture that dispatches inside one
  // never reaches a listener outside, and a test asserting "the gesture stood
  // down" passes because nothing ran at all.
  //
  // `cancelable: true` likewise: a real touchmove is cancelable unless it was
  // already committed to a passive listener, and the hook's page suppression
  // checks that flag before calling preventDefault. Omit it and the suppression
  // correctly does nothing, so the test measures the fixture, not the code.
  const init: TouchEventInit = { bubbles: true, composed: true, cancelable: true }
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
  /** A right-anchored panel runs the same travel with the opposite sign. */
  const CLOSED_RIGHT = 400

  beforeEach(() => {
    el = document.createElement('div')
    document.body.appendChild(el)
    ref = { current: el }
    x = motionValue(0)
    onGestureOpen = vi.fn()
    onSettle = vi.fn()
    Object.defineProperty(window, 'innerWidth', { writable: true, value: 400 })
  })

  function mount(open = false, side: 'left' | 'right' = 'left') {
    return renderHook(() => useDrawerSwipe(ref, {
      enabled: true, side, open, x, onGestureOpen, onSettle,
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

  it('opens from anywhere in the pane, not just an edge band', () => {
    mount()
    // 300px on a 400px viewport — nowhere near the left edge. The predecessor
    // armed only inside 24-120px, which is why the gesture was hard to find:
    // a drag begun mid-screen, where the thumb naturally lands, did nothing.
    fire(el, touch('touchstart', 300))
    fire(el, touch('touchmove', 330))
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
    expect(x.get()).toBe(CLOSED + 30)
  })

  it('leaves the platform forward-swipe band at the FAR bezel alone too', () => {
    mount()
    // Within 24px of the right edge, dragging the direction that WOULD open the
    // left drawer: the far bezel only became reachable once the opening band
    // spanned the pane, and the OS owns that strip for its own forward gesture.
    fire(el, touch('touchstart', 390))
    fire(el, touch('touchmove', 430))
    expect(onGestureOpen).not.toHaveBeenCalled()
    expect(x.get()).toBe(0)
  })

  it('leaves the far bezel alone for the panel anchored THERE as well', () => {
    // The right panel's own opening drag starts near the right edge, which is
    // exactly where the platform's gesture lives — so the dead zone has to hold
    // for the side that most wants to reach past it.
    x.set(CLOSED_RIGHT)
    mount(false, 'right')
    fire(el, touch('touchstart', 390))
    fire(el, touch('touchmove', 200))
    expect(onGestureOpen).not.toHaveBeenCalled()
    expect(x.get()).toBe(CLOSED_RIGHT)
  })

  it('a LEFT panel ignores a leftward drag — direction is what selects a panel', () => {
    mount()
    fire(el, touch('touchstart', 300))
    fire(el, touch('touchmove', 100))
    expect(onGestureOpen).not.toHaveBeenCalled()
    expect(x.get()).toBe(0)
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

  // ── The right-anchored panel: same gesture, mirrored ────────────────────
  // Only the SIGNS differ, so these pin the mirror rather than re-testing the
  // machinery: closed sits at +travel, a leftward drag opens, a rightward one
  // closes, and a flick is judged against this side's own opening direction.

  it('opens a RIGHT panel on a leftward drag, tracking the finger', () => {
    x.set(CLOSED_RIGHT)
    mount(false, 'right')
    fire(el, touch('touchstart', 300))
    fire(el, touch('touchmove', 280))          // dx -20, past AXIS_LOCK
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
    expect(x.get()).toBe(CLOSED_RIGHT - 20)
    fire(el, touch('touchmove', 100))
    expect(x.get()).toBe(CLOSED_RIGHT - 200)
  })

  it('a RIGHT panel ignores a rightward drag', () => {
    x.set(CLOSED_RIGHT)
    mount(false, 'right')
    fire(el, touch('touchstart', 100))
    fire(el, touch('touchmove', 300))
    expect(onGestureOpen).not.toHaveBeenCalled()
    expect(x.get()).toBe(CLOSED_RIGHT)
  })

  it('commits a RIGHT panel past halfway, and never past its own edge', async () => {
    x.set(CLOSED_RIGHT)
    mount(false, 'right')
    fire(el, touch('touchstart', 300, 0, 0))
    fire(el, touch('touchmove', 40, 0, 200))   // dx -260 -> 65% of the travel
    expect(x.get()).toBe(CLOSED_RIGHT - 260)
    fire(el, touch('touchmove', -200, 0, 400)) // dragged well past open
    expect(x.get()).toBe(0)                    // clamped at its rest position
    fire(el, touch('touchend', -200, 0, 600))
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(true))
  })

  it('closes an open RIGHT panel on a rightward drag past halfway', async () => {
    x.set(0)
    mount(true, 'right')
    fire(el, touch('touchstart', 20, 0, 0))
    fire(el, touch('touchmove', 300, 0, 200))
    expect(x.get()).toBe(280)
    fire(el, touch('touchend', 300, 0, 400))
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(false))
  })

  it('reads a flick against the RIGHT panel\'s own opening direction', async () => {
    // Same leftward flick that would be a CLOSE on the left drawer: barely 8%
    // of the travel, so only the velocity branch can commit it.
    x.set(CLOSED_RIGHT)
    mount(false, 'right')
    fire(el, touch('touchstart', 300, 0, 0))
    fire(el, touch('touchmove', 270, 0, 10))
    fire(el, touch('touchmove', 268, 0, 12))   // -1 px/ms, past COMMIT_VELOCITY
    fire(el, touch('touchend', 268, 0, 15))
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(true))
  })

  // ── A panel narrower than the screen ────────────────────────────────────
  // The sessions drawer leaves a strip of chat uncovered, so its travel is its
  // own width. Everything the gesture decides divides by that: leave it at the
  // viewport width and the drag runs past the panel's edge while the commit
  // point sits inboard of the real halfway mark.

  /** Bind with an explicit travel narrower than the 400px viewport. */
  function mountNarrow(open = false) {
    return renderHook(() => useDrawerSwipe(ref, {
      enabled: true, travel: () => 360, open, x, onGestureOpen, onSettle,
    }))
  }

  it('rests closed at its OWN width, not the viewport width', () => {
    mountNarrow()
    fire(el, touch('touchstart', 200))
    fire(el, touch('touchmove', 260))          // dx 60 past the axis lock
    expect(x.get()).toBe(-360 + 60)
  })

  it('clamps a drag at the panel edge that travel names', () => {
    mountNarrow()
    fire(el, touch('touchstart', 200))
    fire(el, touch('touchmove', 900))          // far past open
    expect(x.get()).toBe(0)
  })

  it('measures the commit share against the PANEL, not the screen', () => {
    // A fifth of a 360px panel is 72px; a fifth of the 400px screen would be
    // 80px. 76px therefore commits only if the share divides by the travel it
    // was given.
    mountNarrow()
    fire(el, touch('touchstart', 200, 0, 0))
    fire(el, touch('touchmove', 215, 0, 200))
    fire(el, touch('touchmove', 276, 0, 1000))   // dx 76 of 360
    fire(el, touch('touchend', 276, 0, 1400))    // slow: only distance decides
    return waitFor(() => expect(onSettle).toHaveBeenCalledWith(true))
  })

  it('still refuses a release short of that fifth', async () => {
    mountNarrow()
    fire(el, touch('touchstart', 200, 0, 0))
    fire(el, touch('touchmove', 215, 0, 200))
    fire(el, touch('touchmove', 268, 0, 1000))   // dx 68 of 360
    fire(el, touch('touchend', 268, 0, 1400))
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(false))
  })

  it('asks the same fifth of a CLOSING drag, measured from its own start', async () => {
    // The reformulation this pins. Read as an absolute position instead —
    // "commit while the panel is more than a fifth open" — the same 76px pull
    // leaves this panel 79% open and therefore refuses to close, so a light
    // threshold for opening would have become an 80% threshold for closing.
    x.set(0)
    mountNarrow(true)
    fire(el, touch('touchstart', 300, 0, 0))
    fire(el, touch('touchmove', 285, 0, 200))
    fire(el, touch('touchmove', 224, 0, 1000))   // dx -76 of 360
    fire(el, touch('touchend', 224, 0, 1400))
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(false))
  })

  // ── Horizontal scroller ownership (carried over from useSwipeEdge) ───────
  // A wide code block, a markdown table or a card strip under the finger owns
  // the gesture OUTRIGHT. Losing this makes every horizontal pan inside a
  // message close or open a drawer.
  //
  // It used to be conditional — deferring only while the scroller still had
  // somewhere to go in the drag's direction, so the gesture was handed over at
  // its end the way nested scroll views do. That is right when the parent is
  // itself a scroller and wrong when the parent is a drawer: a freshly rendered
  // code block sits at `scrollLeft === 0`, so the FIRST rightward drag on it had
  // nothing to reveal and opened the drawer instead of scrolling the code. The
  // two cases below are the ones that changed, and they are the common state
  // rather than an edge.

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

  it('does not close over a scroller already at its end', () => {
    // Mirror of the reported defect: a table scrolled to its right edge, dragged
    // left, used to open the side panel because there was nothing further to
    // reveal. The table still owns the axis.
    const sc = appendScroller(600)
    x.set(0)
    mount(true)
    expect(sc.scrollLeft).toBe(sc.scrollWidth - sc.clientWidth)
    fire(sc, touch('touchstart', 200))
    fire(sc, touch('touchmove', 100))
    expect(x.get()).toBe(0)
  })

  it('does not open over a scroller already at its start', () => {
    // The reported defect itself: a code block renders at scrollLeft 0, so the
    // first rightward drag on it has nothing to reveal — and must still scroll
    // the code rather than summon the drawer.
    const sc = appendScroller(0)
    mount()
    fire(sc, touch('touchstart', 200))
    fire(sc, touch('touchmove', 360))
    expect(onGestureOpen).not.toHaveBeenCalled()
    // Untouched: a gesture that never armed does not seat the closed offset
    // either, so the value stays where the consumer left it.
    expect(x.get()).toBe(0)
  })

  it('still opens over content that has nothing to scroll', () => {
    // Deference is owed to a scroller, not to any element inside a message: a
    // code block whose content fits has no axis to own, so the drag is
    // unambiguous and the drawer is still reachable there.
    const fits = appendScroller(0, 300, 300)
    expect(fits.scrollWidth - fits.clientWidth).toBe(0)
    mount()
    fire(fits, touch('touchstart', 200))
    fire(fits, touch('touchmove', 360))
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
  })

  /** A shadow host whose shadow root holds the element that actually scrolls —
   *  the shape of a finished chat code block (Pierre's `diffs-container`).
   *
   *  Dispatched on the HOST with a stubbed `composedPath()`, which is what a real
   *  engine presents: the listener outside the root sees `target === host`
   *  (retargeting), while `composedPath()` still carries the inner node. jsdom
   *  does not retarget on its own, so dispatching straight at the inner node
   *  would leave `e.target` pointing inside the root and let a plain
   *  `parentElement` walk find the scroller — a fixture that cannot fail. */
  function appendShadowScroller(scrolls: boolean): { host: HTMLDivElement; inner: HTMLDivElement } {
    const host = document.createElement('div')
    el.appendChild(host)
    const shadow = host.attachShadow({ mode: 'open' })
    const inner = document.createElement('div')
    if (scrolls) {
      inner.style.overflowX = 'scroll'
      Object.defineProperty(inner, 'scrollWidth', { configurable: true, value: 900 })
      Object.defineProperty(inner, 'clientWidth', { configurable: true, value: 300 })
      Object.defineProperty(inner, 'scrollLeft', { configurable: true, writable: true, value: 0 })
    }
    shadow.appendChild(inner)
    return { host, inner }
  }

  function fireThroughShadow(host: HTMLElement, inner: HTMLElement, e: TouchEvent) {
    Object.defineProperty(e, 'composedPath', {
      value: () => [inner, inner.getRootNode(), host, el, document.body, document, window],
    })
    host.dispatchEvent(e)
  }

  it('finds a scroller INSIDE a shadow root', () => {
    // Why the earlier fix did not help: the deference condition was right, but
    // the scroller was never FOUND. `e.target` is retargeted to the host, so a
    // walk up `parentElement` sees a host with nothing to scroll and concludes
    // there is none. `composedPath()` crosses the boundary.
    const { host, inner } = appendShadowScroller(true)
    mount()
    fireThroughShadow(host, inner, touch('touchstart', 200))
    fireThroughShadow(host, inner, touch('touchmove', 360))
    expect(onGestureOpen).not.toHaveBeenCalled()
    expect(x.get()).toBe(0)
  })

  it('defers when the BOUND element is itself the scroller', () => {
    // Pins the asymmetry between the two chain readers, which is easy to
    // "simplify" away: the scroller search includes `root` (a consumer that binds
    // a horizontally scrollable element gets no gesture — the pre-existing
    // contract), while the ownership search excludes it (an instance does not
    // suppress itself with its own claim).
    el.style.overflowX = 'auto'
    Object.defineProperty(el, 'scrollWidth', { configurable: true, value: 900 })
    Object.defineProperty(el, 'clientWidth', { configurable: true, value: 300 })
    mount()
    fire(el, touch('touchstart', 200))
    fire(el, touch('touchmove', 360))
    expect(onGestureOpen).not.toHaveBeenCalled()
  })

  it('a shadow host with nothing to scroll does not suppress the gesture', () => {
    // Control: crossing the boundary must not turn every web component into a
    // gesture sink.
    const { host, inner } = appendShadowScroller(false)
    mount()
    fireThroughShadow(host, inner, touch('touchstart', 200))
    fireThroughShadow(host, inner, touch('touchmove', 360))
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
  })

  // ── Text-selection ownership ────────────────────────────────────────────
  // A long-press selects a word and puts drag handles on it, and extending the
  // selection rightward is a horizontal drag over plain chat text — dead
  // center in the arming surface, with the handles invisible to both chain
  // readers (they are browser chrome, not elements). Without deference the
  // drawer slid in mid-selection.

  describe('yields to an active text selection', () => {
    afterEach(() => {
      vi.restoreAllMocks()
      // The focused-editable cases leave a focused node behind; removing the
      // bound element takes it out of the document, which resets
      // `document.activeElement` — so no later test inherits the focus.
      el.remove()
    })

    /** What `document.getSelection()` reports; only `isCollapsed` is read. */
    function stubSelection(isCollapsed: boolean) {
      return vi.spyOn(document, 'getSelection').mockReturnValue({ isCollapsed } as Selection)
    }

    it('never arms while a selection is active, however clean the drag', () => {
      stubSelection(false)
      mount()
      fire(el, touch('touchstart', 40))
      fire(el, touch('touchmove', 200))   // far past AXIS_LOCK
      expect(onGestureOpen).not.toHaveBeenCalled()
      expect(x.get()).toBe(0)
    })

    it('a collapsed selection (a caret, or none) changes nothing', () => {
      // The regression pin: getSelection() is rarely null in a real engine —
      // an empty selection reports as a collapsed one — so the guard must key
      // on isCollapsed, not on existence.
      stubSelection(true)
      mount()
      fire(el, touch('touchstart', 40))
      fire(el, touch('touchmove', 60))
      expect(onGestureOpen).toHaveBeenCalledTimes(1)
      expect(x.get()).toBe(CLOSED + 20)
    })

    it('never arms on a touch that begins inside a focused editable', () => {
      // One step earlier than a range: the caret's own handle drags before any
      // selection exists, so the element being typed in owns its touches.
      const ta = document.createElement('textarea')
      el.appendChild(ta)
      ta.focus()
      expect(document.activeElement).toBe(ta)
      mount()
      fire(ta, touch('touchstart', 40))
      fire(ta, touch('touchmove', 200))
      expect(onGestureOpen).not.toHaveBeenCalled()
      expect(x.get()).toBe(0)
    })

    it('an editable that is NOT focused does not suppress the gesture', () => {
      // Focus is the signal: an idle input carries no handle to defer to, and
      // merely containing a form must not kill the swipe across it.
      const ta = document.createElement('textarea')
      el.appendChild(ta)
      expect(document.activeElement).not.toBe(ta)
      mount()
      fire(ta, touch('touchstart', 40))
      fire(ta, touch('touchmove', 60))
      expect(onGestureOpen).toHaveBeenCalledTimes(1)
    })

    it('sees a focused editable INSIDE a shadow root', () => {
      // Outside the root both `e.target` and `document.activeElement` are
      // retargeted to the HOST, so a parentElement walk from the target never
      // meets the editable. The guard crosses the boundary the same way the
      // scroller search does — the composed chain — and the focus side
      // descends through `shadowRoot.activeElement` to the real caret holder.
      const host = document.createElement('div')
      el.appendChild(host)
      const shadow = host.attachShadow({ mode: 'open' })
      const ta = document.createElement('textarea')
      shadow.appendChild(ta)
      ta.focus()
      expect(document.activeElement).toBe(host)   // retargeted — the trap this pins
      expect(shadow.activeElement).toBe(ta)
      mount()
      fireThroughShadow(host, ta, touch('touchstart', 40))
      fireThroughShadow(host, ta, touch('touchmove', 200))
      expect(onGestureOpen).not.toHaveBeenCalled()
      expect(x.get()).toBe(0)
    })

    it('resets when a long-press creates the selection MID-touch', () => {
      // The finger goes down on unselected text, the long-press selects under
      // it, and the same touch drags the handle on without lifting — so the
      // touchstart check saw a collapsed selection and armed. The pending
      // branch re-checks right before locking.
      const spy = stubSelection(true)
      mount()
      fire(el, touch('touchstart', 40))
      spy.mockReturnValue({ isCollapsed: false } as Selection)
      fire(el, touch('touchmove', 200))
      expect(onGestureOpen).not.toHaveBeenCalled()
      expect(x.get()).toBe(0)
      // And it stays declined — reset, not postponed: a later stretch of the
      // same touch must not retroactively claim the gesture.
      fire(el, touch('touchmove', 300))
      expect(onGestureOpen).not.toHaveBeenCalled()
      expect(x.get()).toBe(0)
    })
  })
})

// ── The page stands down for the duration of a locked gesture ──────────────
// The four tracking listeners are passive, so without this the browser keeps
// doing its own thing WHILE the finger drives the panel: the transcript scrolls
// vertically under the drawer, and the release fires a click on whatever the
// drag passed over.
describe('useDrawerSwipe page suppression', () => {
  let el: HTMLDivElement
  let ref: { current: HTMLDivElement }
  let x: ReturnType<typeof motionValue<number>>
  let onGestureOpen: ReturnType<typeof vi.fn>
  let onSettle: ReturnType<typeof vi.fn>
  let clicked: ReturnType<typeof vi.fn>
  let button: HTMLButtonElement

  beforeEach(() => {
    el = document.createElement('div')
    document.body.appendChild(el)
    clicked = vi.fn()
    button = document.createElement('button')
    button.addEventListener('click', clicked)
    el.appendChild(button)
    ref = { current: el }
    x = motionValue(0)
    onGestureOpen = vi.fn()
    onSettle = vi.fn()
    Object.defineProperty(window, 'innerWidth', { writable: true, value: 400 })
  })

  afterEach(() => {
    vi.useRealTimers()
    el.remove()
  })

  function mount(open = false) {
    return renderHook(() => useDrawerSwipe(ref, {
      enabled: true, side: 'left', open, x, onGestureOpen, onSettle,
    }))
  }

  function fire(target: EventTarget, e: TouchEvent) {
    act(() => { target.dispatchEvent(e) })
    return e
  }

  /** A cancelable click, so `defaultPrevented` and the handler both mean
   *  something — the two halves of "the button did not fire". */
  function click(target: EventTarget) {
    const e = new MouseEvent('click', { bubbles: true, cancelable: true })
    act(() => { target.dispatchEvent(e) })
    return e
  }

  it('prevents the page from scrolling once the gesture is locked', () => {
    mount()
    fire(el, touch('touchstart', 40))
    const locking = fire(el, touch('touchmove', 60))
    // The frame that locked escapes: a listener added mid-dispatch only governs
    // SUBSEQUENT events. That is the frame the axis lock spent proving intent.
    expect(locking.defaultPrevented).toBe(false)
    const after = fire(el, touch('touchmove', 200))
    expect(after.defaultPrevented).toBe(true)
  })

  it('leaves a touch that never locks entirely to the browser', () => {
    mount()
    fire(el, touch('touchstart', 40))
    // Vertical: the scroller owns it, and it must keep scrolling.
    const move = fire(el, touch('touchmove', 42, 80))
    expect(move.defaultPrevented).toBe(false)
    const next = fire(el, touch('touchmove', 44, 160))
    expect(next.defaultPrevented).toBe(false)
  })

  it('swallows the click a released drag would fire on a button under the finger', () => {
    mount()
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 60))
    fire(el, touch('touchmove', 240))
    fire(el, touch('touchend', 240))
    const e = click(button)
    expect(clicked).not.toHaveBeenCalled()
    expect(e.defaultPrevented).toBe(true)
  })

  it('swallows only ONE click, so the next genuine tap gets through', () => {
    mount()
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 60))
    fire(el, touch('touchend', 240))
    click(button)
    expect(clicked).not.toHaveBeenCalled()
    click(button)
    expect(clicked).toHaveBeenCalledTimes(1)
  })

  it('stops swallowing after the window elapses, so a later tap is not eaten', () => {
    vi.useFakeTimers()
    mount()
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 60))
    fire(el, touch('touchend', 240))
    // Nothing disarms it here: an engine that suppressed the click itself sends
    // none, so the timer is the only way back. Well past the swallow window.
    act(() => { vi.advanceTimersByTime(2000) })
    click(button)
    expect(clicked).toHaveBeenCalledTimes(1)
  })

  it('declines a DIAGONAL drag the browser has likely already started scrolling', () => {
    // dy under dx, so the "is this vertical?" test passes — but dy alone was
    // already past the platform's scroll slop, so the page is moving and no
    // amount of preventDefault takes the touch back. The band between the slop
    // and dx is exactly where this hook's rule and the browser's disagreed.
    mount()
    fire(el, touch('touchstart', 40, 0))
    fire(el, touch('touchmove', 52, 9))
    expect(onGestureOpen).not.toHaveBeenCalled()
    expect(x.get()).toBe(0)
    // And it stays declined: the gesture is ABANDONED, not merely postponed. The
    // finger's dy can wobble back under the slop, but the scroll it started is
    // already running — so a later clean horizontal stretch of the same touch
    // must not retroactively claim it.
    fire(el, touch('touchmove', 200, 4))
    expect(onGestureOpen).not.toHaveBeenCalled()
    expect(x.get()).toBe(0)
  })

  it('still arms on the small vertical drift a real horizontal swipe carries', () => {
    // The control: a thumb arc is never perfectly straight, and declining it
    // would be a gesture nobody can perform.
    mount()
    fire(el, touch('touchstart', 40, 0))
    fire(el, touch('touchmove', 60, 5))
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
  })

  it('re-opens immediately after a closing swipe, while the settle still runs', () => {
    // The reported defect: swipe shut, swipe straight back open, and the second
    // gesture is intermittently declined. `onSettle` runs in the settle
    // animation's completion callback, so the consumer's `open` prop still reads
    // TRUE for the whole closing slide — and a static prop here models exactly
    // that window. The re-opening drag was judged as an opening drag on an open
    // panel and reset. Direction is immaterial: this one is perfectly clean.
    mount(true)
    fire(el, touch('touchstart', 200))
    fire(el, touch('touchmove', 180))   // leftward: closes a left panel
    fire(el, touch('touchend', 40, 0, 400))
    expect(onSettle).not.toHaveBeenCalled()  // the settle is still in flight

    onGestureOpen.mockClear()
    fire(el, touch('touchstart', 200, 0, 500))
    fire(el, touch('touchmove', 240, 0, 516))
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
  })

  it('adopts an open state the CONSUMER set, so a drag can close a tapped-open panel', () => {
    // The other half of the rule above. A settle commits its own target, but a
    // panel opened by the hamburger never went through one — the prop is the only
    // authority there, so a prop CHANGE must be adopted or the gesture would still
    // believe the panel closed and decline the closing drag.
    const h = renderHook(
      ({ open }: { open: boolean }) => useDrawerSwipe(ref, {
        enabled: true, side: 'left', open, x, onGestureOpen, onSettle,
      }),
      { initialProps: { open: false } },
    )
    act(() => { h.rerender({ open: true }) })
    fire(el, touch('touchstart', 200))
    fire(el, touch('touchmove', 160))   // leftward: closes a left panel
    expect(x.get()).toBeLessThan(0)
  })

  it('suppresses the page again on a second drag inside the click window', () => {
    // The two fixes interact: committing the settle target made an immediate
    // re-open ARM, and the ~350ms click-swallow window is exactly that beat. A
    // suppression parked for the click has already dropped its touchmove
    // listener, so inheriting it left the second drag scrolling the page.
    vi.useFakeTimers()
    mount()
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 60))
    fire(el, touch('touchend', 240))
    // Still inside the click window: the first suppression is parked, not gone.
    act(() => { vi.advanceTimersByTime(50) })
    // The first drag committed the panel OPEN, so the second is the CLOSING
    // direction — "swipe out, swipe straight back in", the same beat.
    fire(el, touch('touchstart', 200, 0, 500))
    fire(el, touch('touchmove', 180, 0, 516))
    const after = fire(el, touch('touchmove', 100, 0, 532))
    expect(after.defaultPrevented).toBe(true)
  })

  it('stops swallowing as soon as a NEW touch begins', () => {
    // The common case, not a rare one: a drag over non-interactive content has
    // its synthetic click suppressed by the locked gesture's own
    // preventDefault, so nothing arrives to disarm the swallower and it stays
    // armed for the whole window — long enough to eat the user's next real tap,
    // which is this feature's core beat (swipe open, then tap something in the
    // drawer). A fresh finger means any pending click belongs to it.
    vi.useFakeTimers()
    mount()
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 60))
    fire(el, touch('touchend', 240))
    // Well inside CLICK_SWALLOW_MS, and no click has arrived to disarm it.
    act(() => { vi.advanceTimersByTime(60) })
    // The user's next deliberate tap: touchstart, then its click.
    fire(el, touch('touchstart', 100, 0, 500))
    fire(el, touch('touchend', 100, 0, 540))
    const e = click(button)
    expect(clicked).toHaveBeenCalledTimes(1)
    expect(e.defaultPrevented).toBe(false)
  })

  it('a plain tap that never became a gesture is never swallowed', () => {
    mount()
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchend', 41))
    click(button)
    expect(clicked).toHaveBeenCalledTimes(1)
  })

  it('releases its window listeners when unbound mid-gesture', () => {
    // These listeners live on `window`, so an unmount that left them armed would
    // keep preventing scroll and eat a tap on whatever replaced the hook.
    const h = mount()
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 60))
    h.unmount()
    const move = fire(el, touch('touchmove', 200))
    expect(move.defaultPrevented).toBe(false)
    click(button)
    expect(clicked).toHaveBeenCalledTimes(1)
  })

  it('releases suppression when the gesture is cancelled rather than released', () => {
    mount()
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 60))
    fire(el, touch('touchcancel', 60))
    const move = fire(el, touch('touchmove', 200))
    expect(move.defaultPrevented).toBe(false)
  })
})
