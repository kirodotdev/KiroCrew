/**
 * The trackpad drag's settle and its mid-drag speed commit (rule C).
 * The phone drawer's own timings live in useDrawerSwipe and are not touched.
 */
import { describe, it, expect } from 'vitest'
import {
  trackpadSettleShape, speedCommitTrackpadDrag, moveTrackpadDrag, idleTrackpadDrag, releaseTrackpadDrag, type TrackpadPanelDrag,
} from './trackpadPanelDrag'

/** A cubic-bezier's progress per step over `n` equal steps of time. */
function speeds([x1, y1, x2, y2]: readonly number[], n = 200): number[] {
  const bz = (a: number, b: number, t: number) => 3 * (1 - t) ** 2 * t * a + 3 * (1 - t) * t * t * b + t ** 3
  const yOfX = (x: number) => {
    let lo = 0
    let hi = 1
    for (let i = 0; i < 60; i++) { const m = (lo + hi) / 2; if (bz(x1, x2, m) < x) lo = m; else hi = m }
    return bz(y1, y2, lo)
  }
  const ys = Array.from({ length: n + 1 }, (_, i) => yOfX(i / n))
  return ys.slice(1).map((y, i) => (y - ys[i]) * n)
}

describe('trackpadSettleShape', () => {
  it('a slow committed release on a wide panel lands within 150ms', () => {
    for (const travel of [460, 818, 1440]) {
      expect(trackpadSettleShape(travel, travel * 0.3, 0).secs).toBeLessThanOrEqual(0.15)
      expect(trackpadSettleShape(0, travel * 0.7, -0.05).secs).toBeLessThanOrEqual(0.15)
    }
  })

  it('never slower than 4px/ms on average, never under the 80ms floor', () => {
    expect(trackpadSettleShape(0, 460, 0).secs).toBeCloseTo(0.115, 6)
    expect(trackpadSettleShape(0, 200, 0).secs).toBe(0.08)
  })

  it('front-loaded: fastest on the first frame, only slower after, most of the way early', () => {
    for (const v of [0, 0.3, 1.2, 8]) {
      const s = speeds(trackpadSettleShape(0, 400, -v).ease)
      for (let i = 1; i < s.length; i++) expect(s[i]).toBeLessThanOrEqual(s[i - 1] + 1e-9)
      const firstThird = s.slice(0, s.length / 3).reduce((a, b) => a + b, 0) / s.length
      expect(firstThird).toBeGreaterThan(0.85)
    }
  })

  it('opens no slower than the release speed', () => {
    for (const v of [0.4, 1, 2.5, 6]) {
      const shape = trackpadSettleShape(0, 400, -v)
      const avg = 400 / (shape.secs * 1000)
      expect(speeds(shape.ease)[0] * avg).toBeGreaterThanOrEqual(v * 0.98)
    }
  })

  it('a hard flick ends sooner than a light swipe', () => {
    expect(trackpadSettleShape(0, 600, -40).secs).toBeLessThan(trackpadSettleShape(0, 600, -0.2).secs)
  })
})

describe('speedCommitTrackpadDrag (rule C)', () => {
  /** A closed 400px panel dragged in by `deltas`, one every `gap` ms. */
  const drag = (deltas: number[], gap = 16): TrackpadPanelDrag => {
    const d = { ...idleTrackpadDrag(), on: true, travel: 400, base: 400 }
    let sum = 0
    deltas.forEach((dx, i) => { sum += dx; moveTrackpadDrag(d, sum, 1000 + i * gap) })
    return d
  }

  it('commits a fast drag toward open at once, with its speed', () => {
    const c = speedCommitTrackpadDrag(drag([10, 10, 10, 10]))
    expect(c).toEqual({ open: true, velocity: expect.any(Number) })
    expect(c!.velocity).toBeCloseTo(-10 / 16, 6)
  })

  it('commits a fast drag toward closed', () => {
    const d = { ...idleTrackpadDrag(), on: true, travel: 400, base: 0, startedOpen: true }
    let sum = 0
    for (let i = 0; i < 4; i++) { sum -= 10; moveTrackpadDrag(d, sum, 1000 + i * 16) }
    expect(speedCommitTrackpadDrag(d)?.open).toBe(false)
  })

  it('never commits a slow drag, however far it goes: distance waits for the release', () => {
    const d = drag(Array(60).fill(3))
    expect(400 - d.samples[d.samples.length - 1].x).toBe(180)
    expect(speedCommitTrackpadDrag(d)).toBeNull()
  })

  it('a single spiky step does not commit', () => {
    expect(speedCommitTrackpadDrag(drag([3, 3, 3, 80, 3]))).toBeNull()
  })

  it('needs two steps over at least 16ms', () => {
    expect(speedCommitTrackpadDrag(drag([30, 30]))).toBeNull()
    expect(speedCommitTrackpadDrag(drag([30, 30, 30], 4))).toBeNull()
  })
})

describe('releaseTrackpadDrag: the projected landing', () => {
  /** An open panel (travel 400) dragged out at `per` px every 16ms, `n` steps. */
  const drag = (per: number, n: number) => {
    const d: TrackpadPanelDrag = { ...idleTrackpadDrag(), on: true, startedOpen: true, travel: 400, base: 0 }
    let x = 0
    for (let i = 0; i < n; i++) x = moveTrackpadDrag(d, -(i + 1) * per, i * 16)
    return { d, x }
  }

  it('a light flick that would coast past the share commits; one that would not falls back', () => {
    // 4px per 16ms (0.25 px/ms, under the commit speed): 40px out, projected
    // 70px (17.5%), short of the 20% share.
    const short = drag(4, 10)
    expect(releaseTrackpadDrag(short.d, short.x, 'momentum', 0).open).toBe(true)
    // 5.5px per 16ms (0.34 px/ms): 55px out (14%), projected about 96px (24%).
    const far = drag(5.5, 10)
    expect(releaseTrackpadDrag(far.d, far.x, 'momentum', 0).open).toBe(false)
  })

  it('fingers that stopped carry no speed: the landing is where they stopped', () => {
    const { d, x } = drag(1, 70)
    // 70px out (17.5%), then a 400ms quiet gap before the release.
    expect(releaseTrackpadDrag(d, x, 'gap', 69 * 16 + 400).open).toBe(true)
  })
})
