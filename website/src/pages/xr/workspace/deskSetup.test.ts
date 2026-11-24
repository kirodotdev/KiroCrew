import { describe, expect, it } from 'vitest'
import { SETUP, SetupClock, flatHand, movesDesk, onADesk, placeOf, type SetupHand } from './deskSetup'

/** A hand whose wrist is at `w`, with its points `lift` m apart in height at most. */
const hand = (w: [number, number, number], lift = 0): SetupHand => ({ wrist: w, points: [w, [w[0], w[1] + lift, w[2] - 0.08], [w[0] + 0.02, w[1], w[2] - 0.17]] })
const L = (dy = 0) => hand([-0.15, 1.2 + dy, -0.4])
const R = (dy = 0) => hand([0.15, 1.2 + dy, -0.4])

describe('the desk\'s set-up by flat hands', () => {
  it('sets the desk after both hands lie flat and still for 1.5 s: at their height, its near edge under the wrists, facing ahead', () => {
    const clock = new SetupClock()
    let last = clock.read(L(), R(), 0)
    expect(last.share).toBe(0)
    for (let t = 100; t < SETUP.stillMs; t += 100) { last = clock.read(L(), R(), t); expect(last.place).toBeNull() }
    last = clock.read(L(), R(), SETUP.stillMs)
    expect(last.share).toBe(1)
    expect(last.place?.at[1]).toBeCloseTo(1.2, 6)
    expect(last.place?.at[2]).toBeCloseTo(-0.4, 6)
    expect(last.place?.yawDeg).toBe(0)
  })

  it('starts again when a hand moves, is not flat, or is lost', () => {
    const clock = new SetupClock()
    clock.read(L(), R(), 0)
    expect(clock.read(L(0.02), R(), 1000).share).toBe(0)
    expect(clock.read(L(0.02), R(), 2400).place).toBeNull()
    expect(flatHand(hand([0, 1, 0], 0.03))).toBe(false)
    expect(clock.read(hand([-0.15, 1.2, -0.4], 0.03), R(), 2600).share).toBe(0)
    expect(clock.read(null, R(), 2700).share).toBe(0)
  })

  it('faces the way the hands lie, and moves the desk only past 2 cm or 3 degrees', () => {
    const turned = placeOf(hand([0, 1.2, -0.3]), hand([0.3 * Math.cos(Math.PI / 6), 1.2, -0.3 + 0.3 * Math.sin(Math.PI / 6)]))
    expect(turned.yawDeg).toBeCloseTo(30, 1)
    const now = { at: [0, 1.2, -0.4] as [number, number, number], yawDeg: 0 }
    expect(movesDesk(now, { at: [0.015, 1.2, -0.4], yawDeg: 2 })).toBe(false)
    expect(movesDesk(now, { at: [0.025, 1.2, -0.4], yawDeg: 0 })).toBe(true)
    expect(movesDesk(now, { at: [0, 1.2, -0.4], yawDeg: 4 })).toBe(true)
    expect(movesDesk({ at: now.at, yawDeg: 179 }, { at: now.at, yawDeg: -179 })).toBe(false)
    expect(movesDesk(null, now)).toBe(true)
  })

  it('takes hands for a desk only ahead of the eyes and under them', () => {
    const eye: [number, number, number] = [0, 1.6, 0]
    expect(onADesk(L(), eye, [0, -1])).toBe(true)
    // Hands parked at the sides and behind, as the emulator parks them; hands held up at the eyes.
    expect(onADesk(hand([-0.3, 1.0, 0.2]), eye, [0, -1])).toBe(false)
    expect(onADesk(hand([0, 1.5, -0.4]), eye, [0, -1])).toBe(false)
    // The head turned to the right: ahead is +x.
    expect(onADesk(hand([0.4, 1.2, 0]), eye, [1, 0])).toBe(true)
  })
})
