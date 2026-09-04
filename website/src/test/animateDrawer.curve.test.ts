/**
 * The drawer settle's CURVES, pinned with the numbers that chose them.
 *
 * The settle is ASYMMETRIC: arriving and leaving are different events.
 *
 * ENTRY — the guard here is a FIRST-FRAME budget, and it is a restored one. It
 * existed, was deleted when a more front-loaded curve was adopted on the theory
 * that front-loading was not what made an earlier shape read wrong, and three
 * device verdicts then said otherwise: easeOutExpo `(0.19, 1, 0.22, 1)` at 340ms
 * (26% of the travel gone in the first painted frame), easeOutQuint
 * `(0.16, 1, 0.3, 1)` at 320ms (30%) and `(0.1, 0.9, 0.2, 1)` at 320ms (39%)
 * were each rejected for reading as the panel appearing rather than sliding. The
 * accepted shape is iOS's sheet curve `(0.32, 0.72, 0, 1)`, which spends 10% —
 * and which `components/OverlayDrawer.tsx` had already been using all along, for
 * the reason its own comment gives: "a strong ease-out front-loads the travel,
 * which visually freezes the near edges while the far edges are still sweeping."
 * So the budget is not a preference, it is the same conclusion reached twice.
 *
 * EXIT — the SAME curve, simply shorter. Its own easeIN (slow off the mark, then
 * quick) was tried on the theory that a dismissal should start from where the
 * panel is; on a device it reads as the panel hesitating before it goes, and it
 * disagreed with the gesture-released dismissal beside it, which has to
 * decelerate because the finger is already moving. So the shape is a property of
 * the surface and the duration is a property of the event: 420ms to disclose,
 * 240ms to dismiss.
 *
 * The two curves are also the Notification Center sheet's, asserted here as one
 * motion language across both files.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import tailwindConfig from '../../tailwind.config.js'

const animateSpy = vi.fn(() => ({ stop: vi.fn() }))
vi.mock('framer-motion', async (importOriginal) => ({
  ...(await importOriginal<typeof import('framer-motion')>()),
  animate: (...args: unknown[]) => animateSpy(...(args as [])),
}))

const { animateDrawer } = await import('../hooks/useDrawerSwipe')
const { motionValue } = await import('framer-motion')

/** Eased progress at `x` for a cubic-bezier, by bisection on its x-polynomial. */
function easedAt(p: readonly number[], x: number): number {
  const [p1, p2, p3, p4] = p
  const cx = (t: number) => 3 * p1 * t * (1 - t) ** 2 + 3 * p3 * t ** 2 * (1 - t) + t ** 3
  const cy = (t: number) => 3 * p2 * t * (1 - t) ** 2 + 3 * p4 * t ** 2 * (1 - t) + t ** 3
  let lo = 0, hi = 1, t = x
  for (let i = 0; i < 40; i++) { t = (lo + hi) / 2; if (cx(t) < x) lo = t; else hi = t }
  return cy(t)
}

const TRAVEL = 390
/** Rest is offset 0, so the target is what picks the direction's curve. */
const settle = (to: number) => {
  animateSpy.mockClear()
  animateDrawer(motionValue(to === 0 ? -TRAVEL : 0), to)
  return animateSpy.mock.calls[0][2] as { ease: readonly number[]; duration: number; type?: string }
}

describe('animateDrawer — settle curves', () => {
  beforeEach(() => {
    animateSpy.mockClear()
    Object.defineProperty(window, 'matchMedia', {
      writable: true, configurable: true,
      value: vi.fn().mockImplementation((q: string) => ({ matches: false, media: q, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
    })
  })

  it('settles on stated cubic-beziers, not springs', () => {
    for (const to of [0, -TRAVEL]) {
      const opts = settle(to)
      expect(opts.type, 'a spring cannot be handed to a KeyframeEffect').toBeUndefined()
      expect(Array.isArray(opts.ease)).toBe(true)
      expect(opts.ease).toHaveLength(4)
      expect(opts.duration).toBeGreaterThan(0)
    }
  })

  it('uses a DIFFERENT curve and duration per direction', () => {
    const inOpts = settle(0)
    const outOpts = settle(-TRAVEL)
    // Two shapes, one family — and the split is about where the settle STARTS,
    // not about the direction as such. Entering, and continuing a released
    // finger, both open at 2.25x the average: for the finger that is continuity,
    // and for an entry the jump lands at the screen edge. A TAP dismissal starts
    // from rest with the panel in full view, so it launches at 1.0x instead.
    expect(outOpts.ease).not.toEqual(inOpts.ease)
    // Both still decelerate into their end — neither accelerates away, which the
    // very first exit shape did and which read as a hesitation.
    expect(outOpts.ease[1] / outOpts.ease[0]).toBeGreaterThanOrEqual(1)
    expect(inOpts.ease[1] / inOpts.ease[0]).toBeGreaterThan(1)
    // The entry front-loads harder: that is the difference being pinned.
    expect(inOpts.ease[1] / inOpts.ease[0]).toBeGreaterThan(outOpts.ease[1] / outOpts.ease[0])
    expect(outOpts.duration).not.toBe(inOpts.duration)
    // The dismissal is the LONGER of the two, and deliberately so: a tap carries
    // no velocity, so it is played as the gentlest release rather than as a
    // hurried exit. ("A dismissal is shorter, nothing is being disclosed" was the
    // earlier rule; judged on a device at 300ms and 400ms it read as rushed
    // against a slow swipe, which lands on the release ceiling.)
    expect(outOpts.duration).toBeGreaterThan(inOpts.duration)
  })

  it('LEAVES fast and glides a long way into the edge', () => {
    const { ease, duration } = settle(-TRAVEL)
    const ms = duration * 1000
    const at = (t: number) => easedAt(ease, Math.min(1, t / ms))
    // Anti-jump, and the reason this curve differs from the entry's: a tap starts
    // from a standstill with the panel in full view, so the FIRST PAINTED FRAME
    // is where a launch and a jump are told apart. The entry's shape put 9% of the
    // travel (31.6px of 350) in that frame and read as skipping ahead; this one
    // spends under 5%.
    expect(at(17)).toBeLessThan(0.05)
    // Front-loaded, not a hesitation: a fifth of the time buys well over a fifth
    // of the travel.
    expect(at(ms / 5)).toBeGreaterThan(0.3)
    // …and the glide is the point: the LAST tenth of the travel gets a third of
    // the duration or more. A near-linear exit fails this, which is what made it
    // read as merely stopping rather than settling.
    const t90 = (() => {
      let lo = 0, hi = 1
      for (let i = 0; i < 60; i++) { const m = (lo + hi) / 2; if (at(m * ms) < 0.9) lo = m; else hi = m }
      return ((lo + hi) / 2) * ms
    })()
    expect(ms - t90).toBeGreaterThan(ms / 3)
    // Decelerating: the first half covers more ground than the second.
    expect(at(ms / 2)).toBeGreaterThan(at(ms) - at(ms / 2))
    expect(at(ms)).toBeCloseTo(1, 2)
  })

  it('arrives by DECELERATING into place, without front-loading the first frame', () => {
    const { ease, duration } = settle(0)
    const ms = duration * 1000
    const at = (t: number) => easedAt(ease, Math.min(1, t / ms))
    // The restored budget. One painted frame must still look like a start: the
    // three shapes rejected on device measure 26%, 30% and 39% here, and the
    // accepted one 10%. Stated against the real 17ms rather than a fraction of
    // the duration, because shortening the duration is itself a way to put more
    // travel in that first frame.
    expect(at(17)).toBeLessThan(0.2)
    // Mirror image of the exit: most of the travel is behind it at half time.
    expect(at(ms / 2)).toBeGreaterThan(0.5)
    expect(at(ms)).toBeCloseTo(1, 2)
  })

  it('is the ONLY spelling of the sheet motion language — no CSS keyframe twin', () => {
    // The Notification Center sheet used to state these curves a SECOND time, as
    // a `nc-slide-in` / `nc-slide-out` Tailwind keyframe pair, because a tailwind
    // config cannot import a TS module — and the test here compared the two
    // spellings to keep them in step. The sheet now settles through
    // `animateDrawer`, so there is one spelling and drift is impossible rather
    // than merely detected.
    //
    // What is worth guarding instead is the shape those keyframes had. A CSS
    // keyframe's `from` is an ABSOLUTE endpoint, so swapping the pair mid-flight
    // teleported the sheet to the incoming animation's origin: measured on a
    // 390px sheet, dismissing 100ms into the entrance jumped it the remaining
    // ~100px to fully-open, and re-opening 50ms into the exit flung it the whole
    // 410px offscreen and replayed the full entrance. Re-introducing either
    // entry is re-introducing that, so their ABSENCE is the assertion.
    const ext = (tailwindConfig as {
      theme: { extend: { animation: Record<string, string>; keyframes: Record<string, unknown> } }
    }).theme.extend
    for (const key of ['nc-slide-in', 'nc-slide-out'] as const) {
      expect(ext.animation[key], `${key} animation must not come back`).toBeUndefined()
      expect(ext.keyframes[key], `${key} keyframes must not come back`).toBeUndefined()
    }
  })

  it('keeps reduced motion on its own short linear tween, both directions', () => {
    Object.defineProperty(window, 'matchMedia', {
      writable: true, configurable: true,
      value: vi.fn().mockImplementation((q: string) => ({ matches: true, media: q, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
    })
    expect(settle(0)).toMatchObject({ ease: 'linear' })
    expect(settle(-TRAVEL)).toMatchObject({ ease: 'linear' })
  })

  /**
   * The compositor path states the curve as a CSS string and the main-thread
   * fallback as a number array. `settleTiming` derives the string from the array
   * so the two cannot disagree — but "derives" is an implementation detail a
   * refactor can quietly replace with a literal, and then a reduced-motion or
   * mount-grace fallback would settle on a different curve from the compositor
   * one with nothing going red. This is the assertion that makes the derivation
   * load-bearing rather than merely intended.
   */
  it('hands the compositor the SAME curve the main-thread fallback uses', async () => {
    const { registerDrawerTargets } = await import('../hooks/useDrawerSwipe')
    const panel = document.createElement('div')
    const timings: Record<string, unknown>[] = []
    ;(panel as unknown as { animate: unknown }).animate = (_k: unknown, timing: Record<string, unknown>) => {
      timings.push(timing)
      return { cancel() {}, onfinish: null, oncancel: null }
    }

    for (const to of [0, -TRAVEL]) {
      // Main-thread spelling first (nothing registered -> framer tween).
      const { ease, duration } = settle(to)

      // …then the compositor spelling for the same direction.
      timings.length = 0
      const x = motionValue(to === 0 ? -TRAVEL : 0)
      const unregister = registerDrawerTargets(x, {
        panel: () => panel, scrim: () => null, travel: () => TRAVEL,
      })
      try {
        animateDrawer(x, to)
      } finally {
        unregister()
      }
      expect(timings, `direction ${to} took the compositor path`).toHaveLength(1)

      const css = /cubic-bezier\(([^)]+)\)/.exec(String(timings[0].easing))
      expect(css, `compositor easing for ${to}: ${timings[0].easing}`).not.toBeNull()
      expect(css![1].split(',').map(Number)).toEqual([...ease])
      expect(timings[0].duration).toBeCloseTo(duration * 1000, 5)
    }
  })
})
