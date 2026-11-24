import { describe, expect, it } from 'vitest'
import {
  PARTS_MAX, PART_STEP_DEG, THROW, atRest, flick, isAway, landing, litOf, openAmount, openRest, partPlace, springStep, takerOf, throwKind,
  type RaySample, type Spring,
} from './handling'
import { TUNE } from './tune'

describe('pulling a thing apart', () => {
  it('opens inside a fixed window of the gap between the two hands: shut under one mark, open at the other', () => {
    expect(openAmount(TUNE.gapCloseM, 'window')).toBe(0)
    expect(openAmount(TUNE.gapOpenM, 'window')).toBe(1)
    expect(openAmount((TUNE.gapCloseM + TUNE.gapOpenM) / 2, 'window')).toBeCloseTo(0.5)
    // The whole move is under five centimetres, and the hands end ten centimetres apart: it works small and low.
    expect(TUNE.gapOpenM - TUNE.gapCloseM).toBeLessThan(0.05)
  })

  it('with the window, reads the same gap the same way wherever the hands took hold: a tremor in a small first gap cannot open it', () => {
    // Hands that took hold 2 cm apart and shake by 6 mm. As a ratio of the first gap that is a third more; in the window it is nothing.
    for (const gap of [0.02, 0.026, 0.014, 0.03]) expect(openAmount(gap, 'window', 0.02)).toBe(0)
    for (const gap of [0.1, 0.106, 0.112]) expect(openAmount(gap, 'window', 0.1)).toBe(1)
  })

  it('keeps the other rule too: twice the gap the hands took hold with is open, half of it is shut', () => {
    // 5 cm to 10 cm, and 40 cm to 80 cm: the same doubling, the same result.
    expect(openAmount(0.1, 'double', 0.05, 0)).toBe(1)
    expect(openAmount(0.8, 'double', 0.4, 0)).toBe(1)
    expect(openAmount(0.05 * Math.SQRT2, 'double', 0.05, 0)).toBeCloseTo(0.5)
    expect(openAmount(0.05, 'double', 0.05, 0)).toBe(0)
    // From open, pushed together.
    expect(openAmount(0.05, 'double', 0.1, 1)).toBe(0)
    expect(openAmount(0.1 / Math.SQRT2, 'double', 0.1, 1)).toBeCloseTo(0.5)
    // Past half way and let go it goes on to open, by either rule.
    expect(openRest(openAmount(0.075, 'double', 0.05, 0))).toBe(1)
    expect(openRest(openAmount(0.08, 'window'))).toBe(1)
  })

  it('never goes past open or shut, and reads a gap that is not a number as no change', () => {
    expect(openAmount(5, 'window')).toBe(1)
    expect(openAmount(0, 'window')).toBe(0)
    expect(openAmount(Number.NaN, 'window')).toBe(0)
    expect(openAmount(50, 'double', 0.05, 0)).toBe(1)
    expect(openAmount(0.0001, 'double', 0.05, 1)).toBe(0)
    expect(openAmount(Number.NaN, 'double', 0.05, 0.3)).toBe(0.3)
    expect(openAmount(0.1, 'double', 0, 0.3)).toBe(0.3)
  })

  it('goes on to open when let go past half way, and shuts before', () => {
    expect(openRest(0.5)).toBe(1)
    expect(openRest(0.49)).toBe(0)
  })

  it('springs open: it overshoots a little, comes back, and settles', () => {
    let s: Spring = { x: 0, v: 0 }
    let peak = 0
    let frames = 0
    while (!atRest(s, 1) && frames < 600) { s = springStep(s, 1, 1 / 60); peak = Math.max(peak, s.x); frames += 1 }
    expect(atRest(s, 1)).toBe(true)
    // Springy, not wobbly: past the end by a little, and at rest within about a second.
    expect(peak).toBeGreaterThan(1.02)
    expect(peak).toBeLessThan(1.25)
    expect(frames).toBeLessThan(90)
  })

  it('stays steady on a slow frame, and eases the same way back to shut', () => {
    let s: Spring = { x: 0, v: 0 }
    for (let i = 0; i < 40; i += 1) { s = springStep(s, 1, 0.25); expect(Math.abs(s.x)).toBeLessThan(1.5) }
    expect(atRest(s, 1)).toBe(true)
    for (let i = 0; i < 200; i += 1) s = springStep(s, 0, 1 / 60)
    expect(atRest(s, 0)).toBe(true)
  })

  it('fans the parts out evenly about the thing, and gathers them in when it shuts', () => {
    const open = Array.from({ length: PARTS_MAX }, (_, i) => partPlace(i, PARTS_MAX, 1))
    expect(open.map(p => p.yaw)).toEqual([-1.5, -0.5, 0.5, 1.5].map(k => k * PART_STEP_DEG))
    expect(open.every(p => p.rise > 0)).toBe(true)
    expect(partPlace(3, PARTS_MAX, 0)).toEqual({ yaw: 0, rise: 0 })
    expect(partPlace(0, 1, 1).yaw).toBe(0)
  })
})

describe('throwing a thing', () => {
  /** A drag that turns at `degPerS` across for `ms`, sampled each 16 ms, then holds still for `restMs`. */
  const drag = (degPerS: number, ms: number, restMs = 0, pitchPerS = 0): RaySample[] => {
    const out: RaySample[] = []
    for (let t = 0; t <= ms; t += 16) out.push({ t, yaw: (degPerS * t) / 1000, pitch: (pitchPerS * t) / 1000 })
    const end = out.at(-1)!
    for (let t = 16; t <= restMs; t += 16) out.push({ t: end.t + t, yaw: end.yaw, pitch: end.pitch })
    return out
  }

  it('reads the speed of the last moment before the thing was let go', () => {
    const f = flick(drag(120, 400))
    expect(f.yaw).toBeCloseTo(120, 0)
    expect(f.speed).toBeCloseTo(120, 0)
    expect(flick(drag(-90, 400, 0, 90)).speed).toBeCloseTo(Math.hypot(90, 90), 0)
  })

  it('counts a thing that was carried fast and then held still as put down', () => {
    expect(flick(drag(300, 300, 200)).speed).toBe(0)
    expect(flick([]).speed).toBe(0)
    expect(flick([{ t: 0, yaw: 0, pitch: 0 }]).speed).toBe(0)
  })

  it('still reads a throw when frames come slower than the moment it looks at', () => {
    // A frame every 150 ms at 60 degrees a second, and the release 10 ms after the last frame.
    const slow: RaySample[] = [0, 150, 300, 450, 460].map(t => ({ t, yaw: (60 * t) / 1000, pitch: 0 }))
    expect(flick(slow).speed).toBeCloseTo(60, 0)
    // 60 degrees a second at reading distance is close to a metre a second: a throw.
    expect(throwKind(flick(slow).speed, 0.9)).toBe('throw')
    // A sample that is too old says nothing about how the hand moved as it let go.
    expect(flick([{ t: 0, yaw: 0, pitch: 0 }, { t: 900, yaw: 40, pitch: 0 }]).speed).toBe(0)
  })

  it('tells a throw from a put-down by the speed of the thing itself: a slow release has one meaning', () => {
    const mps = (v: number, dist: number) => (v / dist) * (180 / Math.PI)
    for (const dist of [0.5, 0.9]) {
      expect(throwKind(mps(TUNE.throwFlyMps * 0.9, dist), dist)).toBe('place')
      expect(throwKind(mps(TUNE.throwFlyMps * 0.4, dist), dist)).toBe('place')
      expect(throwKind(mps(TUNE.throwFlyMps * 1.1, dist), dist)).toBe('throw')
    }
    // The same turn of the wrist moves a far page faster than a near thing: a page at reading distance flies sooner.
    expect(throwKind(60, 0.9)).toBe('throw')
    expect(throwKind(60, 0.5)).toBe('place')
    // A page carried to its place and let go while the hand still drifts, or after it stopped, stays where it was let go.
    expect(throwKind(flick(drag(10, 300)).speed, 0.9)).toBe('place')
    expect(throwKind(flick(drag(300, 300, 200)).speed, 0.9)).toBe('place')
  })

  it('lands a thrown thing further along its way, by its speed, and across the back of the seat', () => {
    expect(landing({ yaw: 10, pitch: -4 }, flick(drag(200, 300)))).toMatchObject({ yaw: expect.closeTo(10 + 200 * THROW.GLIDE_S, 0), pitch: expect.closeTo(-4, 1) })
    expect(landing({ yaw: 10, pitch: -4 }, flick(drag(200, 300))).yaw).toBeGreaterThan(landing({ yaw: 10, pitch: -4 }, flick(drag(100, 300))).yaw)
    // The hardest throw goes a quarter turn and no further, so a page never lands behind the person's back by accident.
    expect(landing({ yaw: 10, pitch: -4 }, flick(drag(900, 300))).yaw).toBeCloseTo(10 + THROW.MAX * THROW.GLIDE_S, 0)
    // A drag that crosses from one side of the back to the other is still one small turn.
    const wrap = flick([{ t: 0, yaw: 175, pitch: 0 }, { t: 100, yaw: -175, pitch: 0 }])
    expect(wrap.yaw).toBeCloseTo(100, 0)
  })

  it('puts away a thing thrown off the workbench: down past the desk, or up over the head', () => {
    expect(isAway(landing({ yaw: 0, pitch: -4 }, flick(drag(0, 300, 0, -200))))).toBe(true)
    expect(isAway(landing({ yaw: 0, pitch: -4 }, flick(drag(0, 300, 0, 200))))).toBe(true)
    expect(isAway(landing({ yaw: 0, pitch: -4 }, flick(drag(300, 300))))).toBe(false)
  })
})

describe('H7 and H10: what a hand can take lights before the pinch, and the pinch takes the lit thing', () => {
  it('lights the very thing under the hand: a Bot, a thing, a marble, the bin or the card', () => {
    // `takes` is what the pinch is given, so the id that lights is the id that is taken.
    const under: { tok?: string; bot?: string; obj?: string }[] = [{ bot: 'juno' }, { obj: 'obj:atlas/thing:w-atlas-flaky' }, { tok: 'marble:a1' }, { tok: 'bin' }, { tok: 'card' }]
    for (const takes of under) {
      expect(litOf(takes, '', '')?.id).toBe(takes.bot ?? takes.obj ?? takes.tok)
    }
    expect(litOf({ bot: 'juno' }, '', '')).toEqual({ kind: 'bot', id: 'juno' })
    expect(litOf({ obj: 'air:1' }, '', '')?.kind).toBe('thing')
    expect(litOf({ tok: 'card' }, '', '')?.kind).toBe('card')
  })

  it('lights a Bot that is pointed at, whatever else would light', () => {
    expect(litOf({ bot: 'juno' }, 'atlas', 'pico')).toEqual({ kind: 'bot', id: 'juno' })
  })

  it('lights nothing when there is nothing to take', () => {
    expect(litOf(null, '', '')).toBeNull()
    expect(litOf({}, '', '')).toBeNull()
  })

  it('with a thing in the hand, lights what would take it: the Bot whose card it is, the Bot in reach, or the bin', () => {
    expect(litOf(null, 'atlas', '')).toEqual({ kind: 'bot', id: 'atlas' })
    expect(litOf(null, '', 'juno')).toEqual({ kind: 'bot', id: 'juno' })
    expect(litOf(null, '', 'bin')).toEqual({ kind: 'token', id: 'bin' })
  })

  it('throws to the Bot that lit, and to no other: the lit Bot is the one that takes the throw', () => {
    const lit = litOf(null, '', 'juno')
    expect(takerOf('throw', 'atlas', lit?.id ?? '', TUNE.throwAimMs)).toBe('juno')
    expect(takerOf('throw', 'atlas', '', 0)).toBe('')
  })
})
