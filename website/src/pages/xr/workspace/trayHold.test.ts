import { describe, expect, it } from 'vitest'
import { DRIFT_EASE_MS, downLetGo, driftStep, trayHolds, upTakes } from './trayHold'
import { driftNow } from './places'

describe('a tray press kept across a leave (D192 2)', () => {
  const down = { id: 7 }
  it.each([
    { name: 'the same pointer, on a drawn piece, a click: takes', at: { pointerId: 7, drawn: true, ending: 'click' as const }, takes: true },
    { name: 'the same pointer, a long press: takes, as today', at: { pointerId: 7, drawn: true, ending: 'long' as const }, takes: true },
    { name: 'the same pointer, off its drawn pieces: nothing', at: { pointerId: 7, drawn: false, ending: 'click' as const }, takes: false },
    { name: 'another pointer: nothing', at: { pointerId: 8, drawn: true, ending: 'click' as const }, takes: false },
    { name: 'a drag past the click\'s reach: nothing, as today', at: { pointerId: 7, drawn: true, ending: 'drag' as const }, takes: false },
  ])('$name', ({ at, takes }) => {
    expect(upTakes(down, at)).toBe(takes)
  })
  it('no down kept: an up takes nothing', () => {
    expect(upTakes(null, { pointerId: 7, drawn: true, ending: 'click' })).toBe(false)
  })
  it.each([
    { name: 'while the pinch is held and the session visible: kept', at: { pinching: true, quiet: false, visible: true }, goes: false },
    { name: 'the pinch is over (its up came elsewhere): let go', at: { pinching: false, quiet: false, visible: true }, goes: true },
    { name: 'the quiet time: let go', at: { pinching: true, quiet: true, visible: true }, goes: true },
    { name: 'a hidden or blurred session: let go', at: { pinching: true, quiet: false, visible: false }, goes: true },
  ])('a kept down, $name', ({ at, goes }) => {
    expect(downLetGo(at)).toBe(goes)
  })
})

describe('a tray under a pointer stands still (D192 1)', () => {
  const at = { pointed: false, down: false, resting: true, carried: false, walking: false }
  it.each([
    { name: 'a pointer on it', x: { ...at, pointed: true }, holds: true },
    { name: 'a down kept on it', x: { ...at, down: true }, holds: true },
    { name: 'nothing on it', x: at, holds: false },
    { name: 'taken (its own motion stays)', x: { ...at, pointed: true, resting: false }, holds: false },
    { name: 'its Bot carried (it goes with it)', x: { ...at, pointed: true, carried: true }, holds: false },
    { name: 'its Bot walking (it goes with it)', x: { ...at, pointed: true, walking: true }, holds: false },
  ])('$name', ({ x, holds }) => {
    expect(trayHolds(x)).toBe(holds)
  })

  it('held still, the drift clock stands; let go, it goes on from there and is back to pace after the ease', () => {
    let c = { t: 10_000, rate: 1 }
    c = driftStep(c, true, 16)
    expect(c).toEqual({ t: 10_000, rate: 0 })
    for (let i = 0; i < 40; i += 1) c = driftStep(c, true, 16)
    expect(c.t).toBe(10_000)
    // No jump: the drift read at the first frame after the let-go is next to where it stood.
    const held = driftNow('mira', c.t)
    c = driftStep(c, false, 16)
    const next = driftNow('mira', c.t)
    expect(Math.hypot(next[0] - held[0], next[1] - held[1])).toBeLessThan(1e-4)
    // 300 ms after the let-go it moves again, at well over half its pace.
    for (let ms = 16; ms < DRIFT_EASE_MS; ms += 16) c = driftStep(c, false, 16)
    expect(c.t).toBeGreaterThan(10_000 + 50)
    expect(c.rate).toBeGreaterThan(0.6)
  })
})

describe('a kept down belongs to one pinch (REVIEW-73 M244)', () => {
  it('must not take: an up of the same pointer in a later pinch, with no down of its own', () => {
    expect(upTakes({ id: 7, pinch: 1 }, { pointerId: 7, drawn: true, ending: 'click', pinch: 2 })).toBe(false)
  })
  it('must take: the up of the pinch that went down, after a leave and a way back', () => {
    expect(upTakes({ id: 7, pinch: 2 }, { pointerId: 7, drawn: true, ending: 'click', pinch: 2 })).toBe(true)
  })
  it('the flat preview has no pinch: its up takes as before', () => {
    expect(upTakes({ id: 7, pinch: null }, { pointerId: 7, drawn: true, ending: null, pinch: null })).toBe(true)
  })
  it('a new pinch lets an older kept down go, though no frame saw no pinch', () => {
    expect(downLetGo({ pinching: true, quiet: false, visible: true, stale: true })).toBe(true)
    expect(downLetGo({ pinching: true, quiet: false, visible: true, stale: false })).toBe(false)
  })
})
