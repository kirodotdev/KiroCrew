import { describe, expect, it } from 'vitest'
import { SPEAK_EDGE, speakFit } from './speakFit'

// The glasses' view: 61 by 41 degrees.
const tan = (deg: number) => Math.tan((deg * Math.PI) / 180)
const view = { tanX: tan(30.5), tanY: tan(20.5) }
const whole = (f: { scale: number; x: number; y: number }, d: number, w: number, h: number) => {
  const [hw, hh] = [(w * f.scale) / 2, (h * f.scale) / 2]
  return Math.abs(f.x) + hw <= d * view.tanX + 1e-9 && Math.abs(f.y) + hh <= d * view.tanY + 1e-9
}

describe('the sheet he speaks on, in his view', () => {
  it('stands over its Bot at full size when it fits', () => {
    const f = speakFit({ low: [0.2, -0.3], d: 2, ...view, w: 0.8, h: 0.6, leastW: 0.42 })
    expect(f).toEqual({ scale: 1, x: 0.2, y: 0 })
  })
  it('is made smaller under the view\'s top, its lower edge still at its Bot', () => {
    // A far Bot high in the view: 3 m ahead, its head 0.45 m over the view's middle.
    const f = speakFit({ low: [-0.2, 0.45], d: 3, ...view, w: 1.2, h: 0.9, leastW: 0.42 })
    expect(f.scale).toBeLessThan(1)
    expect(f.scale).toBeGreaterThan(0.42 / 1.2)
    // Joined: the lower edge is where the Bot holds it, so the face under it is free.
    expect(f.y - (0.9 * f.scale) / 2).toBeCloseTo(0.45, 6)
    expect(f.y + (0.9 * f.scale) / 2).toBeCloseTo(3 * view.tanY * SPEAK_EDGE, 6)
    expect(f.x).toBe(-0.2)
    expect(whole(f, 3, 1.2, 0.9)).toBe(true)
  })
  it('shifts in at a side, not off', () => {
    const d = 2
    const right = speakFit({ low: [1.1, -0.4], d, ...view, w: 0.8, h: 0.6, leastW: 0.42 })
    expect(right.scale).toBe(1)
    expect(right.x).toBeCloseTo(d * view.tanX * SPEAK_EDGE - 0.4, 6)
    const left = speakFit({ low: [-1.1, -0.4], d, ...view, w: 0.8, h: 0.6, leastW: 0.42 })
    expect(left.x).toBeCloseTo(-right.x, 6)
    expect(whole(right, d, 0.8, 0.6) && whole(left, d, 0.8, 0.6)).toBe(true)
  })
  it('never comes down over its Bot: its lower edge is where the Bot holds it, wherever the Bot stands', () => {
    for (const d of [0.9, 1.7, 3.1]) for (const fx of [-1.4, -0.9, 0, 0.5, 1.4]) for (const fy of [-0.9, -0.3, 0.4, 0.9]) {
      const w = Math.min(1.2, Math.max(0.42, (0.5 * d) / 0.75))
      const h = w * 0.75
      const low = [fx * d * view.tanX, fy * d * view.tanY] as const
      const f = speakFit({ low, d, ...view, w, h, leastW: 0.42 })
      expect(f.y - (h * f.scale) / 2).toBeCloseTo(low[1], 9)
      expect(f.scale * w).toBeGreaterThanOrEqual(Math.min(w, 0.42) - 1e-9)
      // Inside at both sides always; under the top whenever its least size fits there.
      expect(Math.abs(f.x) + (w * f.scale) / 2).toBeLessThanOrEqual(d * view.tanX + 1e-9)
      if (low[1] + (h * 0.42) / w <= d * view.tanY * SPEAK_EDGE) expect(whole(f, d, w, h)).toBe(true)
    }
  })
  it('grows upward past the view\'s top when it is too tall at its least size', () => {
    const d = 2
    const f = speakFit({ low: [0, 0.5], d, ...view, w: 0.8, h: 2.4, leastW: 0.42 })
    expect(f.scale).toBeCloseTo(0.42 / 0.8, 9)
    expect(f.y - (2.4 * f.scale) / 2).toBeCloseTo(0.5, 9)
    expect(f.y + (2.4 * f.scale) / 2).toBeGreaterThan(d * view.tanY)
  })
})
