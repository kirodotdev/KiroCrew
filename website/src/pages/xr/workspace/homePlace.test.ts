import { Vector3 } from 'three'
import { afterEach, describe, expect, it } from 'vitest'
import { CLOSE_CLEAR_DEG, LEAN_EYES, homePlace, markSides, stripCut, stripCuts, stripCutsLeaning, stripFit, underStrip, type P3 } from './homePlace'

afterEach(() => { homePlace.strip = null; homePlace.since = null })

const head = new Vector3(0, 1.6, 0)
const pitch = (y: number, z: number) => (Math.atan2(y - 1.6, Math.abs(z)) * 180) / Math.PI

describe('the cards keep clear of Home\'s band, the since line too (the lead\'s 19:13)', () => {
  it('come down under the since line when it stands under the strip', () => {
    homePlace.strip = { center: new Vector3(0, 2.18, -1.69), widthM: 1.24, heightM: 0.085 }
    const box = { yaw0: -10, yaw1: 10, low: pitch(2.02, -1.7), top: pitch(2.06, -1.7) }
    // The strip alone: the cards just under it are clear.
    expect(underStrip(head, box)).toBe(0)
    // The since line under the strip, where the cards are: they come down under it.
    homePlace.since = { center: new Vector3(0, 2.10, -1.71), widthM: 1.24, heightM: 0.06 }
    const drop = underStrip(head, box)
    expect(drop).toBeGreaterThan(0)
    expect(box.top - drop).toBeLessThan(pitch(2.10 - 0.03, -1.71))
  })
})

describe('the strip gives way to a close mark (D54)', () => {
  const strip = { center: new Vector3(0, 2.1, -1.7), widthM: 1.24, heightM: 0.085 }
  it('gives up its right end up to just short of a press area that lies on it, and nothing for one beside it', () => {
    // A press area from x 0.55 to 0.75 at the strip's height and distance: the strip's right end (0.62) lies on it.
    const on = [[0.55, 2.08, -1.7], [0.75, 2.28, -1.7]] as const
    const keep = 1.7 * Math.tan(Math.atan2(0.55, 1.7) - (CLOSE_CLEAR_DEG * Math.PI) / 180)
    expect(stripCut(head, strip, [on])).toBeCloseTo(0.62 - keep, 3)
    // Above the strip, or left of its middle: nothing.
    expect(stripCut(head, strip, [[[0.55, 2.3, -1.7], [0.75, 2.5, -1.7]]])).toBe(0)
    expect(stripCut(head, strip, [[[-0.4, 2.08, -1.7], [-0.2, 2.28, -1.7]]])).toBe(0)
    expect(stripCut(head, strip, [])).toBe(0)
  })
  it('gives up its left end to a press area left of its middle', () => {
    const left = [[-0.7, 2.08, -1.7], [-0.5, 2.28, -1.7]] as const
    const cuts = stripCuts(head, strip, [left])
    const keep = 1.7 * Math.tan(Math.atan2(0.5, 1.7) - (CLOSE_CLEAR_DEG * Math.PI) / 180)
    expect(cuts.right).toBe(0)
    expect(cuts.left).toBeCloseTo(0.62 - keep, 3)
  })
})

// The reviewer's cases (REVIEW-17, `strip.mjs`): the strip 1.24 m wide and 0.085 m high, 1.76 m off and 13 degrees up,
// facing the eye at (0, 1.6, 0) straight ahead, as `Home.tsx` places it; press areas placed by their yaw.
describe('the strip and the close marks, the reviewer\'s cases (smalls 7 and 8)', () => {
  const head = new Vector3(0, 1.6, 0)
  const D = 1.76
  const pitch = (13 * Math.PI) / 180
  const strip = { center: { x: 0, y: 1.6 + D * Math.sin(pitch), z: -D * Math.cos(pitch) }, widthM: 1.24, heightM: 0.085 }
  const flat = D * Math.cos(pitch)
  const y0 = strip.center.y - 0.02
  /** A press area `w` wide and high whose lower left corner stands at yaw `deg` (0 ahead, 90 right, 180 behind), `far` metres off. */
  const mark = (deg: number, w = 0.12, far = 1.83): readonly [P3, P3] => { const a = (deg * Math.PI) / 180; const b = a + w / far; return [[far * Math.sin(a), y0, -far * Math.cos(a)], [far * Math.sin(b), y0 + w, -far * Math.cos(b)]] }
  /** Where a mark lies along the strip, as the eye sees it (metres from the strip's middle). */
  const span = (deg: number, w = 0.12, far = 1.83) => [flat * Math.tan((deg * Math.PI) / 180), flat * Math.tan((deg * Math.PI) / 180 + w / far)]
  const MIN = 0.3 + 0.04
  const fitted = (marks: (readonly [P3, P3])[]) => stripFit(strip.widthM, MIN, stripCuts(head, strip, marks))
  const clear = (f: { widthM: number; shiftM: number }, s: number[]) => f.shiftM + f.widthM / 2 <= s[0] || f.shiftM - f.widthM / 2 >= s[1]

  it('cuts nothing for no mark, a mark right of the strip, to his right, or behind him either side of straight behind', () => {
    for (const marks of [[], [mark(25)], [mark(90)], [mark(170)], [mark(-175)]]) expect(stripCuts(head, strip, marks)).toMatchObject({ left: 0, right: 0 })
  })

  it('cuts nothing for a mark that lies across straight behind him, as a page or a session\'s 4 cm mark (small 7)', () => {
    expect(stripCuts(head, strip, [mark(178)])).toMatchObject({ left: 0, right: 0 })
    expect(stripCuts(head, strip, [mark(179.5, 0.04)])).toMatchObject({ left: 0, right: 0 })
  })

  it('cuts its right end for the front page\'s mark at the strip\'s right end, and both ends for marks at both', () => {
    const one = fitted([mark(17)])
    expect(one.widthM).toBeLessThan(1.24)
    expect(clear(one, span(17))).toBe(true)
    const both = fitted([mark(17), mark(-20)])
    expect(clear(both, span(17)) && clear(both, span(-20))).toBe(true)
    expect(both.widthM).toBeGreaterThan(0.9)
  })

  it('lies on no mark near its middle: one across the middle, one just right of it, or one each side (small 8)', () => {
    for (const marks of [[-1], [1], [1, -5]]) {
      const f = fitted(marks.map(d => mark(d)))
      expect(f.widthM).toBeGreaterThanOrEqual(MIN)
      for (const d of marks) expect(clear(f, span(d))).toBe(true)
    }
    // With a mark each side of the middle there is no room between them: the switch alone stands past one of them.
    const two = fitted([mark(1), mark(-5)])
    expect(two.widthM).toBe(MIN)
  })
})

describe('the strip\'s cut holds for an eye that leans 10 cm (small 9)', () => {
  const head = new Vector3(0, 1.6, 0)
  const D = 1.76
  const pitch = (13 * Math.PI) / 180
  const strip = { center: { x: 0, y: 1.6 + D * Math.sin(pitch), z: -D * Math.cos(pitch) }, widthM: 1.24, heightM: 0.085 }
  const yawOf = (eye: { x: number; z: number }, p: { x: number; z: number }) => (Math.atan2(p.x - eye.x, -(p.z - eye.z)) * 180) / Math.PI
  /** A close mark of a page `far` metres off whose left edge stands 0.3 degrees right of the strip's right end, as the start eye sees it. */
  const markAt = (far: number): readonly [P3, P3] => {
    const a = ((yawOf(head, { x: 0.62, z: strip.center.z }) + CLOSE_CLEAR_DEG) * Math.PI) / 180
    const b = a + 0.12 / far
    // At the strip's own pitch, as the eye sees it: a far page's mark that the strip would cover.
    const y = 1.6 + far * Math.tan(pitch) - 0.03
    return [[far * Math.sin(a), y, -far * Math.cos(a)], [far * Math.sin(b), y + 0.12, -far * Math.cos(b)]]
  }

  it('cuts nothing from the start eye alone, and enough that no leaning eye sees the mark within 0.3 degrees of the strip', () => {
    for (const far of [1.9, 3, 6]) {
      const mark = markAt(far)
      expect(stripCuts(head, strip, [mark]).right).toBeLessThan(0.002)
      const cut = stripCutsLeaning(head, strip, [mark])
      const fit = stripFit(strip.widthM, 0.34, cut)
      const end = { x: fit.shiftM + fit.widthM / 2, z: strip.center.z }
      for (const [dx, dz] of LEAN_EYES) {
        const eye = { x: dx, z: dz }
        const gap = yawOf(eye, { x: mark[0][0], z: mark[0][2] }) - yawOf(eye, end)
        expect(gap).toBeGreaterThan(CLOSE_CLEAR_DEG - 0.02)
      }
    }
  })

  it('leaves a strip with no mark near it whole, from every eye', () => {
    expect(stripCutsLeaning(head, strip, [])).toMatchObject({ left: 0, right: 0 })
  })
})

// The reviewer's `strip2.mjs` (REVIEW-19 smalls 5 and 6): the strip as `Home.tsx` places it, rounding included.
describe('the strip holds its side with a degree of slack, and the switch alone clears a corner handle (D56 4)', () => {
  const head = new Vector3(0, 1.6, 0)
  const D = 1.76
  const pitch = (13 * Math.PI) / 180
  const strip = { center: { x: 0, y: 1.6 + D * Math.sin(pitch), z: -D * Math.cos(pitch) }, widthM: 1.24, heightM: 0.085 }
  const flat = D * Math.cos(pitch)
  const y0 = strip.center.y - 0.02
  /** A press area `w` wide and high whose lower left corner stands at yaw `deg`; `reach`: how far right of it the page's corner handle reaches. */
  const mark = (deg: number, w = 0.12, far = 1.83, reach = 0): readonly [P3, P3] => { const a = (deg * Math.PI) / 180; const b = a + (w + reach) / far; return [[far * Math.sin(a), y0, -far * Math.cos(a)], [far * Math.sin(b), y0 + w, -far * Math.cos(b)]] }
  const up = (v: number) => Math.ceil(v * 100) / 100
  const place = (marks: (readonly [P3, P3])[], held: Map<string, 1 | -1>) => {
    const named = marks.map((m, i) => [`p${i}`, m] as const)
    const got = stripCutsLeaning(head, strip, marks, markSides(head, strip, named, held))
    const p = stripFit(1.24, 0.34, { ...got, rightInner: -up(-got.rightInner), rightOuter: up(got.rightOuter), leftInner: up(got.leftInner), leftOuter: -up(-got.leftOuter) })
    const w = 1.24 - Math.round((1.24 - p.widthM) * 100) / 100
    const s = Math.round(p.shiftM * 1000) / 1000
    return [s - w / 2, s + w / 2]
  }
  const span = (m: readonly [P3, P3]) => { const xs = [m[0], m[1]].map(q => flat * Math.tan(Math.atan2(q[0], -q[2]))); return [Math.min(...xs), Math.max(...xs)] }
  const onIt = (st: number[], m: readonly [P3, P3]) => { const [a, b] = span(m); return Math.min(st[1], b) - Math.max(st[0], a) > 0.001 }

  it('does not flip from end to end while a mark is held at its middle and wobbles 1.6 mm', () => {
    const held = new Map<string, 1 | -1>()
    // The mark comes from the right end towards the middle: it keeps the right side past the middle, within the slack.
    // The mark's middle crosses the strip's middle with its left corner near -1.88 degrees (the mark is 3.76 wide).
    for (let d = 17; d >= -1.84; d -= 0.1) place([mark(d)], held)
    const sides: number[] = []
    const ends: number[] = []
    for (let i = 0; i < 20; i += 1) { const st = place([mark(-1.84 - (i % 2) * 0.08)], held); sides.push(held.get('p0') as number); ends.push(st[1]) }
    expect(new Set(sides)).toEqual(new Set([1]))
    expect(Math.max(...ends) - Math.min(...ends)).toBeLessThan(0.02)
    // Without the held side, the same wobble flips the strip from end to end.
    const flips = [0, 1, 2, 3].map(i => place([mark(-1.84 - (i % 2) * 0.08)], new Map())[1])
    expect(Math.max(...flips) - Math.min(...flips)).toBeGreaterThan(0.3)
    // Moved on a full degree past the middle, it goes over to the other side, once.
    for (let d = -1.9; d >= -3.2; d -= 0.1) place([mark(d)], held)
    expect(held.get('p0')).toBe(-1)
  })

  it('stands the switch alone clear of both marks and of the corner handle of the page it moves past', () => {
    // A corner handle reaches three quarters of a corner (about 3 cm) right of the mark's own area.
    const marks = [mark(1, 0.12, 1.83, 0.03), mark(-5, 0.12, 1.83, 0.03)]
    const st = place(marks, new Map())
    expect(st[1] - st[0]).toBeCloseTo(0.34, 2)
    for (const m of marks) expect(onIt(st, m)).toBe(false)
  })

  it('forgets the side of a mark that is gone', () => {
    const held = new Map<string, 1 | -1>()
    place([mark(17), mark(-20)], held)
    expect([...held.keys()]).toEqual(['p0', 'p1'])
    place([mark(17)], held)
    expect([...held.keys()]).toEqual(['p0'])
  })
})
