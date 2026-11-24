import { describe, expect, it } from 'vitest'
import { TAKEN_DISTANCE_M } from './dash3d'
import { DASH_CAMERAS, DASH_EYE, dashCamera, dashSlug } from './dashView'

const sub = (a: readonly number[], b: readonly number[]) => a.map((v, i) => v - b[i])
const len = (v: readonly number[]) => Math.hypot(...v)
const angleDeg = (a: readonly number[], b: readonly number[]) => (Math.acos((a[0] * b[0] + a[1] * b[1] + a[2] * b[2]) / (len(a) * len(b))) * 180) / Math.PI

describe('view=dash: its fixed cameras', () => {
  it('are three, named, the first the default for any other word', () => {
    expect(DASH_CAMERAS.map(c => c.name)).toEqual(['rest', 'taken', 'taken-aside'])
    expect(dashCamera(null).name).toBe('rest')
    expect(dashCamera('over-the-shoulder').name).toBe('rest')
    expect(dashCamera('taken-aside').name).toBe('taken-aside')
  })

  it('stand at his eye height; at rest and taken straight from his eye: down to the tray, level at the taken form', () => {
    for (const c of DASH_CAMERAS) expect(c.position[1]).toBe(DASH_EYE[1])
    const [rest, taken] = DASH_CAMERAS
    for (const c of [rest, taken]) {
      expect(c.position).toEqual(DASH_EYE)
      expect(sub(c.target, c.position)[2]).toBeLessThan(0)
    }
    expect(sub(rest.target, rest.position)[1]).toBeLessThan(0)
    expect(sub(taken.target, taken.position)[1]).toBe(0)
  })

  it('see the taken form from its own distance, the third 30 degrees aside of the second', () => {
    const [, taken, aside] = DASH_CAMERAS
    expect(aside.target).toEqual(taken.target)
    const flat = (v: readonly number[]) => [v[0], 0, v[2]]
    expect(len(flat(sub(taken.position, taken.target)))).toBeCloseTo(TAKEN_DISTANCE_M, 6)
    expect(len(flat(sub(aside.position, aside.target)))).toBeCloseTo(TAKEN_DISTANCE_M, 6)
    expect(angleDeg(flat(sub(taken.position, taken.target)), flat(sub(aside.position, aside.target)))).toBeCloseTo(30, 6)
    expect(DASH_CAMERAS.filter(c => c.taken).map(c => c.name)).toEqual(['taken', 'taken-aside'])
  })
})

describe('view=dash: the slug the address names', () => {
  it('is a Bot slug or nothing', () => {
    expect(dashSlug('atlas')).toBe('atlas')
    expect(dashSlug('taps-xr')).toBe('taps-xr')
    for (const bad of [null, '', 'Atlas', '../members', 'a/b', 'a?b', 'x'.repeat(65), '-a']) expect(dashSlug(bad)).toBeNull()
  })
})
