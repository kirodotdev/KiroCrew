import { describe, expect, it } from 'vitest'
import { TOUCH, TouchClock, nearestM, pointing, targetUnder, type TouchChange, type TouchHand, type TouchTarget } from './deskTouch'

const FRAME = 1000 / 90
/** A pointing hand whose index tip is `h` over a desk at height 0; `flat` lays its middle and ring tips beside it. */
const at = (h: number, flat = false): TouchHand => ({ palm: [0, h + 0.05, 0.08], index: [0, h, 0], middle: [0.02, flat ? h : h + 0.04, 0.05], ring: [0.04, flat ? h : h + 0.04, 0.05] })
type Step = { h: number | null; on?: string | null; flat?: boolean }
const hold = (h: number | null, ms: number, more: Partial<Step> = {}): Step[] => Array.from({ length: Math.max(1, Math.round(ms / FRAME)) }, () => ({ h, ...more }))
const ramp = (from: number, to: number, ms: number): Step[] => { const n = Math.max(1, Math.round(ms / FRAME)); return Array.from({ length: n }, (_, i) => ({ h: from + ((to - from) * (i + 1)) / n })) }
/** Runs the frames over the pad `seat:mira`; says every change. */
const run = (frames: Step[], may = true): TouchChange[] => {
  const c = new TouchClock()
  const out: TouchChange[] = []
  frames.forEach((f, i) => {
    const ch = c.read(f.h === null ? null : at(f.h, f.flat), f.h ?? Infinity, f.on === undefined ? 'seat:mira' : f.on, f.h ?? Infinity, i * FRAME, may)
    if (ch) out.push(ch)
  })
  return out
}

describe('a finger\'s touch on the desk, frame by frame at 90 Hz', () => {
  it('presses a pad when the index tip comes down from over 3 cm, stays 60 ms within 1.5 cm, and leaves past 2.5 cm within 800 ms', () => {
    expect(run([...hold(0.06, 300), ...ramp(0.06, 0.008, 80), ...hold(0.008, 150), ...ramp(0.008, 0.05, 80)])).toEqual([{ down: 'seat:mira' }, { up: 'seat:mira', press: true }])
  })
  it('takes no down from a tip that stays under 1.5 cm for less than 60 ms', () => {
    expect(run([...hold(0.06, 300), ...ramp(0.06, 0.012, 50), ...hold(0.012, 30), ...ramp(0.012, 0.05, 50)])).toEqual([])
  })
  it('is no press when it lifts after 800 ms, or off another pad', () => {
    expect(run([...hold(0.06, 300), ...ramp(0.06, 0.008, 80), ...hold(0.008, 900), ...ramp(0.008, 0.05, 80)])).toEqual([{ down: 'seat:mira' }, { up: 'seat:mira', press: false }])
    expect(run([...hold(0.06, 300), ...ramp(0.06, 0.008, 80), ...hold(0.008, 150), ...ramp(0.008, 0.05, 80).map(s => ({ ...s, on: 'seat:juno' }))])).toEqual([{ down: 'seat:mira' }, { up: 'seat:mira', press: false }])
  })
  it('takes no down from a tip that did not come from over 3 cm, from a hand found low, or from a flat hand', () => {
    expect(run([...hold(0.02, 500), ...ramp(0.02, 0.008, 60), ...hold(0.008, 300)])).toEqual([])
    // Tracked for under 200 ms when it comes down and lifts.
    expect(run([...hold(0.06, 60), ...ramp(0.06, 0.008, 40), ...hold(0.008, 70), ...ramp(0.008, 0.05, 30)])).toEqual([])
    expect(run([...hold(0.06, 300, { flat: true }), ...ramp(0.06, 0.008, 80).map(s => ({ ...s, flat: true })), ...hold(0.008, 300, { flat: true })])).toEqual([])
  })
  it('takes no down over no pad, nor while another press owns the button; a hand lost while down calls it off', () => {
    expect(run([...hold(0.06, 300), ...ramp(0.06, 0.008, 80).map(s => ({ ...s, on: null })), ...hold(0.008, 300, { on: null })])).toEqual([])
    expect(run([...hold(0.06, 300), ...ramp(0.06, 0.008, 80), ...hold(0.008, 300)], false)).toEqual([])
    expect(run([...hold(0.06, 300), ...ramp(0.06, 0.008, 80), ...hold(0.008, 150), { h: null }])).toEqual([{ down: 'seat:mira' }, { cancel: 'seat:mira' }])
  })
  it('says a hand points only with its index out and its middle and ring tips 2 cm over it', () => {
    expect(pointing(at(0.01))).toBe(true)
    expect(pointing(at(0.01, true))).toBe(false)
    expect(TOUCH.nearM).toBe(0.05)
  })
  it('finds the pad under a tip across the desk', () => {
    const targets = new Map<string, TouchTarget>([['seat:mira', { center: [0, 1, -0.6], radiusM: 0.045, press: () => undefined, box: { halfM: 0.045, heightM: 0.112, yawDeg: 0 } }], ['tile:plus', { center: [-0.4, 1, -0.6], radiusM: 0.0425, press: () => undefined, box: { halfM: 0.0425, heightM: 0.05, yawDeg: 0 } }]])
    expect(targetUnder([0.03, 1.01, -0.6], targets)).toBe('seat:mira')
    expect(targetUnder([-0.2, 1.01, -0.6], targets)).toBeNull()
  })

  it('is near a pad from any side: a tip 3 cm beside it at the desk\'s height is near, 8 cm away is not; a hand that does not point is never near', () => {
    const pad: TouchTarget = { center: [0, 1, -0.6], radiusM: 0.045, press: () => undefined, box: { halfM: 0.045, heightM: 0.112, yawDeg: 0 } }
    expect(nearestM([0.045 + 0.03, 1.005, -0.6], pad)).toBeCloseTo(0.03, 6)
    expect(nearestM([0.045 + 0.08, 1.005, -0.6], pad)).toBeCloseTo(0.08, 6)
    expect(nearestM([0, 1.2, -0.6], pad)).toBeCloseTo(0.2 - 0.112, 6)
    expect(nearestM([0.01, 1.05, -0.6], pad)).toBe(0)
    const c = new TouchClock()
    c.read(at(0.005), 0.005, null, 0.03, 0, true)
    expect(c.near).toBe(true)
    c.read(at(0.005), 0.005, null, 0.08, 20, true)
    expect(c.near).toBe(false)
    c.read(at(0.005, true), 0.005, null, 0.0, 40, true)
    expect(c.near).toBe(false)
  })
})
