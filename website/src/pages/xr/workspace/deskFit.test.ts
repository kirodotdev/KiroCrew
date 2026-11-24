import { describe, expect, it } from 'vitest'
import { DESK_FIT, besideClear, besideOrUnder, deskPlace, deskWords, underAt } from './deskFit'

const head = [0, 1.6, 0] as const
// Home's strip and the since line over it, 1.7 m in front of him (as the room of places stands them).
const band = [{ center: [0, 2.09, -1.71] as const, size: [1.24, 0.085] as const }, { center: [0, 2.17, -1.69] as const, size: [1.24, 0.06] as const }]
const near = 2 * Math.atan2(0.16 / 2, 1.8)
// The strip's small words: 0.03 m at 1.71 m.
const floor = 0.03 / 1.71
const gap = DESK_FIT.gapDeg * (Math.PI / 180)

describe('a desk line (D39, ruling 4)', () => {
  it('keeps its whole size over its Bot when it is clear of Home and no wider than a near desk', () => {
    expect(deskPlace(head, [2, 1.7, -2.5], { widthM: 0.16 * 1.6, heightM: 0.06 * 1.6, letterM: 0.03 }, near, floor, band)).toEqual({ share: 1, side: 0 })
  })
  it('is never wider to his eye than a near desk\'s line', () => {
    const { share } = deskPlace(head, [2.5, 1.7, -3], { widthM: 0.4, heightM: 0.1, letterM: 0.05 }, near, floor, [])
    expect(2 * Math.atan2((0.4 * share) / 2, Math.hypot(2.5, 3))).toBeCloseTo(near, 6)
  })
  it('grows smaller until its top is under Home\'s band, its lower edge where its Bot holds it', () => {
    const low = [-0.4, 2.3, -3.6] as const
    const { share, side } = deskPlace(head, low, { widthM: 0.5, heightM: 0.3, letterM: 0.15 }, 1, floor, band)
    expect(side).toBe(0)
    expect(share).toBeLessThan(1)
    const top = Math.atan2(low[1] + 0.3 * share - head[1], Math.hypot(low[0], low[2]))
    expect(top).toBeLessThanOrEqual(Math.atan2(2.09 - 0.085 / 2 - head[1], 1.71) - gap + 1e-9)
  })
  it('never grows smaller than the strip\'s small words: then it stands beside its Bot, away from Home', () => {
    // A Bot whose head is in the band itself, left of the middle: no room over the head at a size he can read.
    const left = deskPlace(head, [-0.3, 2.55, -3.6], { widthM: 0.5, heightM: 0.3, letterM: 0.15 }, 1, floor, band)
    expect(left.side).toBe(-1)
    expect((0.15 * left.share) / Math.hypot(0.3, 3.6)).toBeGreaterThanOrEqual(floor - 1e-9)
    expect((0.15 * left.share) / Math.hypot(0.3, 3.6)).toBeCloseTo(floor * DESK_FIT.besideOverFloor, 9)
    const right = deskPlace(head, [0.3, 2.55, -3.6], { widthM: 0.5, heightM: 0.3, letterM: 0.15 }, 1, floor, band)
    expect(right.side).toBe(1)
  })
  it('beside the band\'s end it grows only as small as keeps its side off the band, above the floor', () => {
    const low = [-1.17, 2.3, -2.6] as const
    const { share, side } = deskPlace(head, low, { widthM: 0.5, heightM: 0.3, letterM: 0.15 }, 1, floor, band)
    expect(side).toBe(0)
    expect(share).toBeLessThan(1)
    const flat = Math.hypot(low[0], low[2])
    const right = Math.atan2(low[0], -low[2]) + Math.atan2((0.5 * share) / 2, flat)
    expect(right).toBeLessThanOrEqual(-Math.atan2(0.62, 1.71) - gap + 1e-9)
  })
  it('stays beside its Bot only within reach of it, else stands under it (the lead\'s 19:45)', () => {
    expect(besideOrUnder(0)).toBe('beside')
    // Its near edge at the reach, 0.4 m from the Bot's middle: still beside.
    expect(besideOrUnder(DESK_FIT.reachM - DESK_FIT.besideM)).toBe('beside')
    // Pushed out farther by the band or another line: under its Bot.
    expect(besideOrUnder(DESK_FIT.reachM - DESK_FIT.besideM + 0.01)).toBe('under')
    expect(besideOrUnder(1.3)).toBe('under')
  })
  it('under its Bot it stands at its desk\'s height, where the Bot stands', () => {
    expect(underAt([0.5, 2.3, -3], { feet: [0.5, 1.0, -3.02], deskY: 1.3 })).toEqual([0.5, 1.3, -3])
    expect(underAt([0.5, 2.3, -3], { feet: [0.5, 1.0, -3.02] })).toEqual([0.5, 1.0 + DESK_FIT.underM, -3])
  })
  it('a line beside its Bot moves out past Home\'s band too, when a Bot was dragged up behind the strip', () => {
    // The band, as he sees it from his head; the line beside its Bot reaches into its left end.
    const strip = { y0: -0.35, y1: 0.35, p0: 0.25, p1: 0.3 }
    const box = { y0: -0.42, y1: -0.3, p0: 0.26, p1: 0.29 }
    const shift = besideClear(box, -1, [strip])
    expect(-0.3 - shift).toBeLessThanOrEqual(strip.y0 - gap + 1e-12)
  })
  it('a line beside its Bot moves out until it lies on no other desk line', () => {
    const box = { y0: -0.3, y1: -0.2, p0: 0.1, p1: 0.15 }
    expect(besideClear(box, -1, [])).toBe(0)
    const other = { y0: -0.35, y1: -0.25, p0: 0.12, p1: 0.17 }
    const shift = besideClear(box, -1, [other])
    expect(-0.2 - shift).toBeLessThanOrEqual(other.y0 - gap + 1e-12)
    // Lines that do not meet in pitch leave it where it is.
    expect(besideClear(box, -1, [{ ...other, p0: 0.3, p1: 0.35 }])).toBe(0)
  })
})

describe('a desk line says whose it is (D42)', () => {
  it('the name first, as on the sheet\'s head', () => {
    expect(deskWords('Mira', 'done · 8:07 PM')).toBe('Mira · done · 8:07 PM')
    expect(deskWords('Mira', 'stopped · 8:07 PM')).toBe('Mira · stopped · 8:07 PM')
    expect(deskWords('', 'done · 8:07 PM')).toBe('done · 8:07 PM')
  })
})
