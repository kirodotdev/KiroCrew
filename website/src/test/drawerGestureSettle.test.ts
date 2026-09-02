/**
 * A settle the FINGER released, as distinct from one a tap started.
 *
 * The reported defect: throwing the drawer shut hard played the same slow
 * dismissal as nudging it shut, and that dismissal's curve is an easeIN — so the
 * harder it was thrown, the more the panel stalled before racing away, which
 * reads as the panel refusing to follow the finger.
 *
 * Two properties fix it, and both are pinned here:
 *  - the curve DECELERATES for a released gesture in either direction, because
 *    the panel is already moving when the finger lets go;
 *  - the duration is derived from the RELEASE SPEED, so a harder flick lands
 *    sooner — the thing a fixed duration cannot express.
 *
 * The tap path is deliberately untouched, and `animateDrawer.curve.test.ts`
 * still pins its direction-asymmetric curves. The last case here is the seam
 * between them.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const animateSpy = vi.fn(() => ({ stop: vi.fn() }))
vi.mock('framer-motion', async (importOriginal) => ({
  ...(await importOriginal<typeof import('framer-motion')>()),
  animate: (...args: unknown[]) => animateSpy(...(args as [])),
}))

const { animateDrawer } = await import('../hooks/useDrawerSwipe')
const { motionValue } = await import('framer-motion')

const TRAVEL = 390
/** The tap durations, which bound the gesture ones from above. */
const TAP_IN_SECS = 0.42
const TAP_OUT_SECS = 0.45
const MIN_SECS = 0.12
const MAX_SECS = 0.45
/** The travel the tap durations were judged on, and which they scale against. */
const REFERENCE_PX = 350
/** `y1/x1` of the decelerating curve (0.32, 0.72, 0, 1): its opening speed as a
 *  multiple of its average. */
const SLOPE = 0.72 / 0.32

interface Opts { ease: readonly number[]; duration: number; type?: string }

/** Release the panel at `from`, heading to `to`, at `velocity` px/ms. */
function release(from: number, to: number, velocity: number): Opts {
  animateSpy.mockClear()
  animateDrawer(motionValue(from), to, undefined, velocity)
  return animateSpy.mock.calls[0][2] as Opts
}

/** The same settle with no release — the tap path. */
function tap(from: number, to: number): Opts {
  animateSpy.mockClear()
  animateDrawer(motionValue(from), to)
  return animateSpy.mock.calls[0][2] as Opts
}

describe('animateDrawer — a finger-released settle', () => {
  beforeEach(() => {
    animateSpy.mockClear()
    Object.defineProperty(window, 'matchMedia', {
      writable: true, configurable: true,
      value: vi.fn().mockImplementation((q: string) => ({ matches: false, media: q, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
    })
  })

  it('decelerates when thrown SHUT, and launches harder than the tap does', () => {
    // A thrown close CONTINUES a finger already moving, so it keeps the sharp
    // curve — its first frame is continuity, not a jump. A tapped close starts
    // from rest, where that same frame reads as skipping ahead, so it launches
    // gently instead. Both still glide into the edge.
    const thrown = release(-40, -TRAVEL, -6)
    const tapped = tap(-40, -TRAVEL)
    expect(thrown.ease).not.toEqual(tapped.ease)
    expect(thrown.ease[1] / thrown.ease[0]).toBeGreaterThan(2)
    expect(tapped.ease[1] / tapped.ease[0]).toBeLessThan(2)
    expect(thrown.duration).toBeLessThan(tapped.duration)
  })

  it('plays a tap dismissal as the SLOWEST release, not as its own number', () => {
    // The tap duration IS the release ceiling — one quantity, so a crawling
    // release and a tap come out identical. Spelling it as a second literal is
    // the drift this pins: tune the band and a duplicated tap silently stays
    // behind.
    const crawl = release(-40, -TRAVEL, -0.05)   // asks for ~15s, clamps to the ceiling
    const tapped = tap(-40, -TRAVEL)
    expect(crawl.duration).toBeCloseTo(tapped.duration, 5)
    expect(tapped.duration).toBeCloseTo(MAX_SECS, 5)
  })

  it('still derives a FIRM release well inside that ceiling', () => {
    // The band is not collapsed onto the tap: speed still buys time.
    const firm = release(-40, -TRAVEL, -2.5)
    expect(firm.duration).toBeLessThan(tap(-40, -TRAVEL).duration)
    expect(firm.duration * 1000).toBeCloseTo(350 * SLOPE / 2.5, 5)
  })

  it('decelerates when thrown OPEN too — same reason, same shape', () => {
    const thrown = release(-TRAVEL + 40, 0, 2.5)
    expect(thrown.ease[1] / thrown.ease[0]).toBeCloseTo(SLOPE, 5)
  })

  it('lands sooner the harder it is thrown', () => {
    // Same remaining distance (350px), three release speeds — all fast enough
    // that the slow bound is not what decides them.
    const soft = release(-40, -TRAVEL, -4)
    const hard = release(-40, -TRAVEL, -5)
    const harder = release(-40, -TRAVEL, -6)
    expect(hard.duration).toBeLessThan(soft.duration)
    expect(harder.duration).toBeLessThan(hard.duration)
  })

  it('opens at the speed the finger let go of it', () => {
    // The continuity condition: distance * slope / speed, in the band where
    // neither bound binds. 200px at 3 px/ms -> 150ms.
    const { duration } = release(-200, 0, 3)
    expect(duration * 1000).toBeCloseTo(200 * SLOPE / 3, 5)
  })

  it('never cuts below the floor, however violent the flick', () => {
    const { duration } = release(-40, -TRAVEL, -40)
    expect(duration).toBeCloseTo(MIN_SECS, 5)
  })

  it('never drags on past the ceiling, however limp the release', () => {
    const { duration } = release(-40, -TRAVEL, -0.05)
    expect(duration).toBeCloseTo(MAX_SECS, 5)
  })

  it('a crawling release and a hard throw are FAR apart, not both on a bound', () => {
    // The reported defect: capping the slow end at the tap duration (0.24s) left
    // a band an ordinary gesture never crossed, so a slow drag and a firm throw
    // played the same animation. The band has to be wide enough that the two are
    // plainly different motions.
    const crawl = release(-40, -TRAVEL, -1)
    const thrown = release(-40, -TRAVEL, -6)
    expect(crawl.duration).toBeGreaterThan(thrown.duration * 2.5)
    // Both readings come from the derivation, not from a clamp collapsing them.
    expect(crawl.duration).toBeCloseTo(MAX_SECS, 5)      // 787ms asked, ceiling
    expect(thrown.duration * 1000).toBeCloseTo(350 * SLOPE / 6, 5)
  })

  it('falls back to the slow end when the release carried no speed', () => {
    // A hold-then-lift: the gesture zeroes its velocity, and there is no
    // momentum to continue.
    const held = release(-40, -TRAVEL, 0)
    expect(held.duration).toBeCloseTo(TAP_OUT_SECS, 5)
    // Curve too, not just duration: starting from rest is exactly the case the
    // sharp reveal curve reads as a snap in.
    expect(held.ease).toEqual(tap(-40, -TRAVEL).ease)
  })

  it('ignores a flick pointing AWAY from where the panel is going', () => {
    // Committed on distance while the finger had already turned back: taking
    // the magnitude would shorten the settle on the strength of a flick in the
    // opposite direction. With no usable speed it times exactly like a tap over
    // the same distance — which is now distance-scaled, so it is compared to one
    // rather than to the reference duration.
    const away = release(-TRAVEL + 200, 0, -3)   // heading to 0, finger going -x
    expect(away.duration).toBeCloseTo(tap(-TRAVEL + 200, 0).duration, 5)
  })

  it('scales a tap duration by the distance it has to cover', () => {
    // The complaint this answers: the nav drawer travels 231px against the
    // sessions drawer's 350px, so one shared duration moved it 1.46x slower per
    // pixel and read as crawling. Held as a SPEED, the two come out
    // proportional — same average px/ms, whatever the panel's width.
    const reference = tap(0, -REFERENCE_PX)
    const narrow = tap(0, -231)
    expect(reference.duration).toBeCloseTo(TAP_OUT_SECS, 5)
    expect(narrow.duration * 1000).toBeCloseTo(TAP_OUT_SECS * 1000 * 231 / REFERENCE_PX, 5)
    // The point of it: equal speed, not equal time.
    expect(REFERENCE_PX / reference.duration).toBeCloseTo(231 / narrow.duration, 5)
  })

  it('never exceeds the reference duration, however wide the panel', () => {
    // The full-width right overlay travels more than the reference, and must not
    // become slower than the slowest reading of its own event.
    expect(tap(0, -600).duration).toBeCloseTo(TAP_OUT_SECS, 5)
    expect(tap(-600, 0).duration).toBeCloseTo(TAP_IN_SECS, 5)
  })

  it('holds a very short settle at the same floor a release uses', () => {
    // An interrupted gesture returning a 20px nudge to rest asks for ~26ms.
    expect(tap(-20, 0).duration).toBeCloseTo(MIN_SECS, 5)
  })

  it('resolves the safe-area inset through a probe, and caches it', async () => {
    const { safeAreaLeft } = await import('../hooks/useDrawerSwipe')
    // jsdom resolves `env()` to nothing, so a panel at `left-safe` measures 0 —
    // the value that must come out when there is no notch, rather than NaN from
    // an unresolved expression.
    expect(safeAreaLeft()).toBe(0)

    // Leaves nothing in the document. Measured across a call that REALLY probes:
    // the resize invalidates the memo first, otherwise a cached call appends
    // nothing and the assertion passes whether or not the node is removed.
    const before = document.body.childElementCount
    dispatchEvent(new Event('resize'))
    expect(safeAreaLeft()).toBe(0)
    expect(document.body.childElementCount).toBe(before)

    // Cached between invalidations — the scrim's binding reads the travel once
    // per frame of a drag, so probing per call would append and remove a node
    // every frame. Counted at the source rather than by looking for leftovers,
    // which a probe that removes itself would pass either way.
    const spy = vi.spyOn(document, 'createElement')
    for (let i = 0; i < 20; i++) safeAreaLeft()
    expect(spy).not.toHaveBeenCalled()
    // …and re-measured on BOTH events that can move it: a resize, and an
    // orientation change, which is the one that actually swings a notch from the
    // top of the screen to the side of it.
    dispatchEvent(new Event('resize'))
    safeAreaLeft()
    expect(spy).toHaveBeenCalledTimes(1)
    dispatchEvent(new Event('orientationchange'))
    safeAreaLeft()
    expect(spy).toHaveBeenCalledTimes(2)
    spy.mockRestore()
  })

  it('leaves the tap path on its own two fixed settings', () => {
    // The seam: no release velocity means the tap curve AND duration for that
    // direction, which animateDrawer.curve.test.ts pins in full. Only a RELEASE
    // derives a duration from anything.
    const closing = tap(0, -TRAVEL)
    const opening = tap(-TRAVEL, 0)
    expect(closing.duration).toBeCloseTo(TAP_OUT_SECS, 5)
    expect(opening.duration).toBeCloseTo(TAP_IN_SECS, 5)
    expect(closing.ease).not.toEqual(opening.ease)
  })

  it('hands the compositor the gesture timing, not the tap timing', async () => {
    // The compositor path resolves the shape separately (it has to read the
    // offset AFTER taking the value over), so it can drift from the fallback.
    const { registerDrawerTargets } = await import('../hooks/useDrawerSwipe')
    const panel = document.createElement('div')
    document.body.appendChild(panel)
    const timings: Record<string, unknown>[] = []
    ;(panel as unknown as { animate: unknown }).animate = (_k: unknown, timing: Record<string, unknown>) => {
      timings.push(timing)
      return { cancel() {}, onfinish: null, oncancel: null }
    }
    const x = motionValue(-40)
    const unregister = registerDrawerTargets(x, {
      panel: () => panel, scrim: () => null, travel: () => TRAVEL,
    })
    animateDrawer(x, -TRAVEL, undefined, -6)
    await new Promise(r => requestAnimationFrame(() => r(null)))
    expect(timings).toHaveLength(1)
    expect(timings[0].easing).toBe('cubic-bezier(0.32, 0.72, 0, 1)')
    // 350px at 6 px/ms, not the 240ms a tap would have taken.
    expect(timings[0].duration).toBeCloseTo(350 * SLOPE / 6, 5)
    unregister()
    panel.remove()
  })
})

describe('useDrawerSwipe — the release hands its own velocity on', () => {
  beforeEach(() => {
    animateSpy.mockClear()
    Object.defineProperty(window, 'innerWidth', { writable: true, value: TRAVEL })
    Object.defineProperty(window, 'matchMedia', {
      writable: true, configurable: true,
      value: vi.fn().mockImplementation((q: string) => ({ matches: false, media: q, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
    })
  })

  /** Close an open left drawer by dragging to `endX` over `ms`, then releasing
   *  `liftAfter` ms after the last move. */
  async function closeByDrag(endX: number, ms: number, liftAfter = 4): Promise<Opts> {
    const { renderHook, act } = await import('@testing-library/react')
    const { useDrawerSwipe } = await import('../hooks/useDrawerSwipe')
    const el = document.createElement('div')
    document.body.appendChild(el)
    const ref = { current: el }
    const x = motionValue(0)
    renderHook(() => useDrawerSwipe(ref, {
      enabled: true, open: true, x, onGestureOpen: () => {}, onSettle: () => {},
    }))
    const fire = (type: string, clientX: number, timeStamp: number) => {
      const t = { clientX, clientY: 0 } as Touch
      const init: TouchEventInit = { bubbles: true }
      if (type === 'touchend') init.changedTouches = [t]
      else init.touches = [t]
      const e = new TouchEvent(type, init)
      Object.defineProperty(e, 'timeStamp', { value: timeStamp })
      act(() => { el.dispatchEvent(e) })
    }
    animateSpy.mockClear()
    fire('touchstart', 380, 0)
    fire('touchmove', 300, 4)
    fire('touchmove', endX, ms)
    fire('touchend', endX, ms + liftAfter)
    el.remove()
    return animateSpy.mock.calls[0][2] as Opts
  }

  it('a flicked close lands sooner than a dragged one', async () => {
    // Identical end position, so the remaining distance is identical too —
    // only the speed the finger arrived with differs. Without the release being
    // threaded through to the settle, both take the same fixed dismissal time.
    const flicked = await closeByDrag(120, 20)   // ~11 px/ms over the window
    const dragged = await closeByDrag(120, 400)  // ~0.45 px/ms
    expect(flicked.duration).toBeLessThan(dragged.duration)
    // And it decelerates rather than replaying the tap's easeIn.
    expect(flicked.ease[1] / flicked.ease[0]).toBeCloseTo(SLOPE, 5)
    // The slow one is the DERIVED ceiling, not the no-speed fallback: its only
    // sample inside the window sits 4ms before the lift, so the measurement has
    // to widen its base past the window to read the drag at all. Without that
    // widening the span is one 4ms step with no displacement, which reads as a
    // dead stop and collapses onto the tap duration.
    expect(dragged.duration).toBeCloseTo(MAX_SECS, 5)
  })

  it('keeps a flick whose lift lands a frame late', async () => {
    // The defect this replaced: the release speed was the last PAIR of samples,
    // discarded outright when touchend arrived more than ~one frame after the
    // last touchmove. A real flick's lift routinely lands later than that, and
    // when it did the whole throw was thrown away and the panel settled at the
    // slow fallback — indistinguishable from a gentle drag, which is exactly the
    // reported symptom. Measured across a window, the lift's timing no longer
    // decides whether the throw counts.
    const prompt = await closeByDrag(120, 20, 4)
    const late = await closeByDrag(120, 20, 40)
    expect(late.duration).toBeCloseTo(prompt.duration, 5)
    expect(late.duration).toBeLessThan(TAP_OUT_SECS)
  })

  it('still refuses to inherit a flick the finger had already stopped', async () => {
    // The hold case the cliff was there for: moved fast, then held still well
    // past the window before lifting. No sample inside the window, so no speed —
    // and therefore a plain tap dismissal over the distance that is left. The
    // drag ends at -260 of a 390 travel, so that is 130px to cover.
    const held = await closeByDrag(120, 20, 400)
    expect(held.duration).toBeCloseTo(tap(-260, -TRAVEL).duration, 5)
  })
})
