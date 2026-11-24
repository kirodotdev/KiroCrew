import { describe, expect, it } from 'vitest'
import { CARRY, REACH, carryM, carryTurn, dragFor, dragOf, inHisFrame, sizeFactor } from './carry'

describe('how far the hand carries a page (D65, P15)', () => {
  const W = 1.56 // a 1024 px page, in metres
  it('carries one to one for the first 2 cm, then the page\'s own width at 15 cm, with a gain that grows with the travel', () => {
    expect(carryM(0, W)).toBe(0)
    // Under 6 mm a resting hand's tremble: nothing. From there one to one.
    expect(carryM(CARRY.stillM, W)).toBe(0)
    expect(carryM(0.01, W)).toBeCloseTo(0.01 - CARRY.stillM, 9)
    expect(carryM(CARRY.exactM, W) - carryM(0.015, W)).toBeCloseTo(0.005, 9)
    expect(carryM(CARRY.fullM, W)).toBeCloseTo(W, 9)
    // The gain grows: each centimetre carries more than the one before it.
    const steps = [0.03, 0.05, 0.08, 0.11, 0.15].map(d => carryM(d, W) - carryM(d - 0.01, W))
    for (let i = 1; i < steps.length; i += 1) expect(steps[i]).toBeGreaterThan(steps[i - 1])
    // Past 15 cm it goes on at the gain it had there.
    expect(carryM(0.17, W) - carryM(0.16, W)).toBeCloseTo(carryM(0.16, W) - carryM(0.15, W), 6)
  })
  it('the other side: a hand at rest (3 mm) and a 1 cm nudge stay under 1 cm and 2 cm', () => {
    expect(carryM(0.003, W)).toBe(0)
    expect(carryM(Math.hypot(0.003, 0.003), W)).toBe(0)
    expect(carryM(0.01, W)).toBeLessThan(0.02)
  })
  it('turns the dot so that the page, 1.75 m away, goes as far as `carryM` says, the hand\'s way', () => {
    const [yaw, pitch] = carryTurn([0.15, 0, 0], 1.75, W)
    expect(-yaw * 1.75).toBeCloseTo(W, 6)
    expect(pitch).toBe(0)
    const [y2, p2] = carryTurn([0, -0.01, 0.2], 1.75, W)
    expect(y2).toBeCloseTo(0, 12)
    expect(p2 * 1.75).toBeCloseTo(-(0.01 - CARRY.stillM), 9)
    expect(carryTurn([0, 0, 0.08], 1.75, W)).toEqual([0, 0])
  })
  it('tells a drag near and far from a move across, and keeps what it became (D67)', () => {
    // Toward him 15 cm: near and far, the distance halved; away 15 cm: doubled.
    const toward = dragOf([0, 0, 0.15], null)
    expect(toward.kind).toBe('depth')
    expect(toward.factor).toBeCloseTo(0.5, 9)
    expect(dragOf([0, 0, -0.15], null).factor).toBeCloseTo(2, 9)
    // Under 4 cm toward him: not yet either, the distance as it is.
    expect(dragOf([0, 0, 0.03], null)).toMatchObject({ kind: null, factor: 1 })
    // Across 15 cm: a move, the distance kept.
    expect(dragOf([0.15, 0, 0], null)).toMatchObject({ kind: 'across', factor: 1 })
    // A diagonal pull, 10 cm across and 6 cm back: a move, not near and far.
    expect(dragOf([0.1, 0, 0.06], null)).toMatchObject({ kind: 'across', factor: 1 })
    // A hand at rest: nothing.
    expect(dragOf([0.003, 0.002, 0.003], null)).toMatchObject({ kind: null, factor: 1 })
    // What a drag became, it stays: a move that is then pulled back changes no distance, and near and far that then
    // goes across moves nothing across (its factor still the travel along the ray).
    expect(dragOf([0.02, 0, 0.15], 'across')).toMatchObject({ kind: 'across', factor: 1 })
    expect(dragOf([0.1, 0, 0.15], 'depth')).toMatchObject({ kind: 'depth' })
    // In his frame: looking 60 degrees to his left, 8 cm back toward him (the room's x and z both) is near and far.
    const yaw60 = Math.PI / 3
    const back60 = [0.08 * Math.sin(yaw60), 0, 0.08 * Math.cos(yaw60)] as const
    expect(dragOf(inHisFrame(back60, yaw60), null).kind).toBe('depth')
    // One pinch keeps one kind: once across, a later pull is across too.
    const pinch = {}
    expect(dragFor(pinch, [0.02, 0, 0]).kind).toBe('across')
    expect(dragFor(pinch, [0.02, 0, 0.15]).kind).toBe('across')
  })

  it('reads the hand in his frame at the down: to his right is right, back toward him is back (D68)', () => {
    // Looking straight ahead, his frame is the room's.
    expect(inHisFrame([0.1, 0.02, 0.03], 0)).toEqual([0.1, 0.02, 0.03])
    // Looking 60 and 90 degrees to his left: 15 cm to HIS right carries the page its width, as straight ahead.
    for (const deg of [60, 90]) {
      const y = (deg * Math.PI) / 180
      const right = [0.15 * Math.cos(y), 0, -0.15 * Math.sin(y)] as const
      const mine = inHisFrame(right, y)
      expect(mine[0]).toBeCloseTo(0.15, 9)
      expect(mine[2]).toBeCloseTo(0, 9)
      const [yaw] = carryTurn(mine, 1.75, W)
      expect(-yaw * 1.75).toBeCloseTo(W, 6)
      // 8 cm back toward him moves nothing across.
      const back = inHisFrame([0.08 * Math.sin(y), 0, 0.08 * Math.cos(y)], y)
      expect(Math.hypot(back[0], back[1])).toBeCloseTo(0, 9)
      expect(carryTurn(back, 1.75, W)).toEqual([0, 0])
    }
  })
  it('stops the carry 100 degrees from where he looked, and 60 up or down (X4)', () => {
    const [y25] = carryTurn([0.25, 0, 0], 1.75, W)
    expect(-y25).toBeCloseTo(REACH.yawRad, 9)
    const [, p25] = carryTurn([0, 0.25, 0], 1.75, W)
    expect(p25).toBeCloseTo(REACH.pitchRad, 9)
    // At 15 cm it does not stop yet.
    expect(-carryTurn([0.15, 0, 0], 1.75, W)[0]).toBeLessThan(REACH.yawRad)
  })
  it('sizes nothing under the still band: a tap or a tremble on a corner (X1)', () => {
    expect(sizeFactor(0.005, 1.9)).toBe(1)
    expect(sizeFactor(-0.006, 1.9)).toBe(1)
  })
  it('a corner drag of 15 cm doubles the page, 15 cm the other way halves it, the first 2 cm one to one', () => {
    const D = Math.hypot(1.56, 1.09)
    expect(sizeFactor(0, D)).toBe(1)
    expect(sizeFactor(0.01, D)).toBeCloseTo(1 + (0.01 - CARRY.stillM) / D, 9)
    expect(sizeFactor(-0.02, D)).toBeCloseTo(1 - (0.02 - CARRY.stillM) / D, 9)
    expect(sizeFactor(CARRY.fullM, D)).toBeCloseTo(2, 9)
    expect(sizeFactor(-CARRY.fullM, D)).toBeCloseTo(0.5, 9)
    expect(sizeFactor(0.08, D)).toBeGreaterThan(sizeFactor(0.05, D))
    expect(sizeFactor(0.003, D) * 1024 - 1024).toBeLessThan(2)
  })
})
