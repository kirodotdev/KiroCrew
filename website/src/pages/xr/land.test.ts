import { describe, expect, it } from 'vitest'
import {
  BOT_M, BOT_MIN_M, DESK, PLOT_M, assignPlots, botHeight, follow, landBounds, landPose, minScale, panBy, parsePlots, plotAt, plotCentre,
  handZoom, scaleAt, zoomFling, settleT, spiralCell, visitorSpot, zoomBy, type LandView,
} from './land'
import { TUNE } from './tune'

describe('plots: a stable rule', () => {
  it('winds a square spiral outward with no cell used twice', () => {
    const cells = Array.from({ length: 60 }, (_, i) => spiralCell(i).join())
    expect(cells.slice(0, 5)).toEqual(['0,0', '1,0', '1,1', '0,1', '-1,1'])
    expect(new Set(cells).size).toBe(60)
  })

  it('never moves a Bot that has a plot: not when Bots are added, removed, or listed in another order', () => {
    const first = assignPlots({}, ['atlas', 'juno', 'mira'])
    expect(first).toEqual({ atlas: 0, juno: 1, mira: 2 })
    const more = assignPlots(first, ['zed', 'mira', 'atlas', 'juno', 'otto'])
    expect(more).toMatchObject(first)
    expect(more.zed).toBe(3)
    expect(more.otto).toBe(4)
    // Juno is gone from the roster for a while. Its cell is kept, so nobody moves into it and it comes back to it.
    const without = assignPlots(more, ['atlas', 'mira', 'otto', 'pico'])
    expect(without).toMatchObject(more)
    expect(without.pico).toBe(5)
    expect(assignPlots(without, ['juno']).juno).toBe(1)
  })

  it('hands back the very same map when nobody is new, so nothing is stored again', () => {
    const map = assignPlots({}, ['a', 'b'])
    expect(assignPlots(map, ['b', 'a'])).toBe(map)
  })

  it('reads a stored map, and drops one that is not a map of distinct whole cells', () => {
    expect(parsePlots('{"a":0,"b":3}')).toEqual({ a: 0, b: 3 })
    for (const bad of ['', 'x', '[1]', '{"a":0,"b":0}', '{"a":-1}', '{"a":1.5}', '{"a":"0"}', 'null']) expect(parsePlots(bad)).toEqual({})
  })

  it('finds the plot a land point is in, and where a visitor stands in it', () => {
    const map = assignPlots({}, ['a', 'b', 'c'])
    expect(plotCentre(map.b)).toEqual([PLOT_M, 0])
    expect(plotAt(map, ['a', 'b', 'c'], PLOT_M + 1, -1)).toBe('b')
    expect(plotAt(map, ['a', 'c'], PLOT_M + 1, -1)).toBe('a')
    const [vx, vz] = visitorSpot(map.b)
    expect(vx).toBe(PLOT_M)
    expect(vz).toBeGreaterThan(0)
    expect(vz).toBeLessThan(PLOT_M / 2)
  })
})

describe('the god view: one continuous move from a miniature to full size', () => {
  const slugs = ['a', 'b', 'c', 'd', 'e', 'f']
  const map = assignPlots({}, slugs)
  const b = landBounds(map, slugs)
  const sMin = minScale(b.radius)
  const EYE = 1.2
  const start: LandView = { t: 0, fx: b.cx, fz: b.cz }

  it('shows the whole land on the desk when zoomed all the way out', () => {
    const pose = landPose(start, sMin, EYE)
    expect(pose.s).toBeCloseTo(sMin)
    expect(pose.y).toBeCloseTo(DESK.y)
    for (const s of slugs) {
      const [x, z] = plotCentre(map[s])
      const wx = pose.x + x * pose.s
      const wz = pose.z + z * pose.s
      expect(Math.hypot(wx, wz - DESK.z)).toBeLessThanOrEqual(DESK.across / 2 + 1e-9)
    }
  })

  it('ends at full size with the person standing at the plot they looked at, on its ground', () => {
    const toward = visitorSpot(map.e)
    let v = start
    for (let i = 0; i < 400 && v.t < 1; i += 1) v = zoomBy(v, 1.03, sMin, toward)
    expect(v.t).toBe(1)
    expect(v.fx).toBeCloseTo(toward[0], 6)
    expect(v.fz).toBeCloseTo(toward[1], 6)
    const pose = landPose(v, sMin, EYE)
    expect(pose.s).toBe(1)
    // The visitor spot is under the person, and the ground is the floor.
    expect(pose.x + toward[0]).toBeCloseTo(0)
    expect(pose.z + toward[1]).toBeCloseTo(0)
    expect(pose.y).toBeCloseTo(-EYE)
  })

  it('has no cut anywhere: a small pull of the hands is a small change of the land, all the way', () => {
    const toward = visitorSpot(map.c)
    let v = start
    let prev = landPose(v, sMin, EYE)
    let steps = 0
    while (v.t < 1 && steps < 2000) {
      v = zoomBy(v, 1.01, sMin, toward)
      const pose = landPose(v, sMin, EYE)
      // The scale grows by at most the pull itself, and the looked-at plot moves by centimetres, never jumps.
      expect(pose.s / prev.s).toBeLessThanOrEqual(1.0101)
      const at = (p: typeof pose) => [p.x + toward[0] * p.s, p.y, p.z + toward[1] * p.s]
      const [a, c] = [at(prev), at(pose)]
      expect(Math.hypot(c[0] - a[0], c[1] - a[1], c[2] - a[2])).toBeLessThan(0.03)
      prev = pose
      steps += 1
    }
    expect(v.t).toBe(1)
    expect(steps).toBeGreaterThan(100)
  })

  it('goes back out the same way, and pushing past either end does nothing', () => {
    let v: LandView = { t: 1, fx: 3, fz: 2 }
    for (let i = 0; i < 400 && v.t > 0; i += 1) v = zoomBy(v, 0.97, sMin, null)
    expect(v).toEqual({ t: 0, fx: 3, fz: 2 })
    expect(zoomBy(v, 0.5, sMin, null)).toEqual(v)
    expect(zoomBy({ t: 1, fx: 0, fz: 0 }, 2, sMin, [9, 9])).toEqual({ t: 1, fx: 0, fz: 0 })
    expect(zoomBy(v, Number.NaN, sMin, null)).toBe(v)
  })

  it('with the window, lets two hands zoom the whole way with a small pull, measured in metres wherever they took hold', () => {
    const span = TUNE.gapOpenM - TUNE.gapCloseM
    for (const small of [0.03, 0.1, 0.4]) {
      for (const first of [0.02, 0.055, 0.2]) {
        // Thirty small moves that add up to the window are the whole way, from any first gap.
        let v: LandView = { t: 0, fx: 0, fz: 0 }
        for (let i = 0; i < 30; i += 1) v = zoomBy(v, handZoom(first + (span * i) / 30, first + (span * (i + 1)) / 30, small, 'window'), small, null)
        expect(v.t).toBeCloseTo(1, 6)
        expect(zoomBy({ t: 1, fx: 0, fz: 0 }, handZoom(first + span, first, small, 'window'), small, null).t).toBeCloseTo(0, 6)
      }
    }
    // A slip of the first gap changes nothing: 6 mm is the same small step from any start.
    const step = (first: number) => zoomBy({ t: 0.3, fx: 0, fz: 0 }, handZoom(first, first + 0.006, 0.03, 'window'), 0.03, null).t - 0.3
    expect(step(0.02)).toBeCloseTo(step(0.2), 9)
    expect(handZoom(0.1, 0.1, 0.03, 'window')).toBe(1)
    expect(handZoom(0.1, 0.2, 1, 'window')).toBe(1)
    expect(handZoom(0.1, Number.NaN, 0.03, 'window')).toBe(1)
  })

  it('keeps the other rule too: the scale goes with the ratio of the gap, by a gain', () => {
    expect(handZoom(0.05, 0.1, 0.03, 'double')).toBeCloseTo(Math.pow(2, TUNE.zoomGain), 9)
    expect(handZoom(0.4, 0.8, 0.03, 'double')).toBeCloseTo(handZoom(0.05, 0.1, 0.03, 'double'), 9)
    expect(handZoom(0.1, 0.05, 0.03, 'double') * handZoom(0.05, 0.1, 0.03, 'double')).toBeCloseTo(1, 9)
    expect(handZoom(0, 0.1, 0.03, 'double')).toBe(1)
  })

  it('lets a zoom that is let go while it still moves carry on to the end, and one let go at rest stay', () => {
    // 20 frames of a steady pull: a tenth of the zoom every 100 ms is one whole zoom a second.
    const pull = (perS: number, stopMs = 0) => {
      const s = Array.from({ length: 20 }, (_, i) => ({ at: i * 16, t: (perS * i * 16) / 1000 }))
      const end = s.at(-1)!
      for (let at = 16; at <= stopMs; at += 16) s.push({ at: end.at + at, t: end.t })
      return s
    }
    expect(zoomFling(pull(1))).toBe(1)
    expect(zoomFling(pull(-1).map(x => ({ at: x.at, t: 1 + x.t })))).toBe(-1)
    expect(zoomFling(pull(TUNE.zoomFlingPerS * 0.8))).toBe(0)
    // Fast, then held still for longer than the moment that is judged: the land stays where it is.
    expect(zoomFling(pull(2, TUNE.zoomFlingMs + 50))).toBe(0)
    // Fast, with one still frame at the very end: the fastest moment still counts.
    expect(zoomFling(pull(2, 32))).toBe(1)
    expect(zoomFling([])).toBe(0)
    expect(zoomFling([{ at: 0, t: 0.4 }])).toBe(0)
  })

  it('makes equal pulls feel equal: the scale is a power of the zoom step', () => {
    expect(scaleAt(0, sMin)).toBeCloseTo(sMin)
    expect(scaleAt(1, sMin)).toBe(1)
    expect(scaleAt(0.5, sMin) / scaleAt(0.25, sMin)).toBeCloseTo(scaleAt(0.75, sMin) / scaleAt(0.5, sMin))
  })

  it('pans with one hand: the ground follows the hand, and the focus stays on the land', () => {
    const s = scaleAt(0, sMin)
    const v = panBy(start, 0.1, -0.05, sMin, b)
    expect(v.fx).toBeCloseTo(start.fx - 0.1 / s)
    expect(v.fz).toBeCloseTo(start.fz + 0.05 / s)
    const far = panBy(start, -50, 0, sMin, b)
    expect(Math.hypot(far.fx - b.cx, far.fz - b.cz)).toBeCloseTo(b.radius)
    expect(panBy(start, 0.1, 0, sMin, b).t).toBe(0)
  })

  it('keeps the horizon level: the land only scales and slides, and its ground only sinks as it grows', () => {
    let last = Infinity
    for (let t = 0; t <= 1.0001; t += 0.05) {
      const pose = landPose({ t, fx: 0, fz: 0 }, sMin, EYE)
      expect(Object.keys(pose).sort()).toEqual(['s', 'x', 'y', 'z'])
      expect(pose.y).toBeLessThanOrEqual(last + 1e-12)
      last = pose.y
    }
  })

  it('eases in and out: the shown view closes a share of the gap each moment, and reduced motion jumps', () => {
    const goal: LandView = { t: 1, fx: 4, fz: 4 }
    const a = follow(start, goal, 0.016)
    const b2 = follow(a, goal, 0.016)
    expect(a.t).toBeGreaterThan(0)
    expect(a.t).toBeLessThan(0.2)
    expect(b2.t - a.t).toBeLessThan(a.t - start.t)
    expect(follow(start, goal, 0.016, true)).toEqual(goal)
    let v = start
    for (let i = 0; i < 400; i += 1) v = follow(v, goal, 0.016)
    expect(v).toEqual(goal)
  })

  it('comes to rest at an end when let go near one, and stays put in between', () => {
    expect(settleT(0.05)).toBe(0)
    expect(settleT(0.95)).toBe(1)
    expect(settleT(0.5)).toBe(0.5)
  })

  it('never draws a Bot too small to find: tiny in the miniature, full height in its plot', () => {
    expect(botHeight(1)).toBe(BOT_M)
    expect(botHeight(sMin)).toBeGreaterThanOrEqual(BOT_MIN_M)
    // Seen from the seat, a Bot in the miniature is still about three degrees tall.
    const deg = (Math.atan(botHeight(sMin) / Math.hypot(DESK.y, DESK.z)) * 180) / Math.PI
    expect(deg).toBeGreaterThan(2.5)
  })
})
