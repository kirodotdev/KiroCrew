import { Object3D, Vector3 } from 'three'
import { describe, expect, it } from 'vitest'
import { GLOW } from './character'
import { DESK, assignPlots, landBounds, landPose, minScale, plotCentre } from './land'
import { AURA_VIEW, DEG, FULL } from './metrics'
import { freePlace, pagePos } from './pages'
import { FULL_H, FULL_W } from './panels'
import { XrSound } from './sound'

describe('how bright a Bot is', () => {
  it('lets only a Bot that asks be at full brightness; an idle Bot has no ring at all', () => {
    expect(GLOW.idle.ring).toBe(0)
    for (const state of ['idle', 'working', 'failed'] as const) {
      expect(GLOW[state].ring).toBeLessThan(GLOW.needs.ring * 0.7)
      expect(GLOW[state].rim).toBeLessThan(GLOW.needs.rim * 0.5)
    }
  })
})

describe('what the glasses show at once', () => {
  /** The corners of a surface that stands at `at` and faces the head, as every page and the count do. */
  const corners = (at: Vector3, w: number, h: number) => {
    const o = new Object3D()
    o.position.copy(at)
    o.lookAt(0, 0, 0)
    o.updateMatrixWorld()
    return [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([x, y]) => o.localToWorld(new Vector3((x * w) / 2, (y * h) / 2, 0)))
  }
  /** Whether a point is inside the view of a head that looks `deg` below level. The view is a rectangle, so its corners hold less. */
  const inside = (p: Vector3, deg: number) => {
    const c = Math.cos(deg * DEG)
    const s = Math.sin(deg * DEG)
    const depth = -p.y * s - p.z * c
    const up = p.y * c - p.z * s
    return depth > 0 && Math.abs(p.x / depth) <= Math.tan((AURA_VIEW.h / 2) * DEG) && Math.abs(up / depth) <= Math.tan((AURA_VIEW.v / 2) * DEG)
  }
  /** The head poses, in tenths of a degree below level, at which every point is in view. */
  const poses = (pts: Vector3[]) => Array.from({ length: 801 }, (_, i) => (i - 100) / 10).filter(deg => pts.every(p => inside(p, deg)))

  it('takes the view from the vendor numbers: 70 degrees diagonal on 1920 x 1200', () => {
    expect(AURA_VIEW.h).toBeCloseTo(61.4, 1)
    expect(AURA_VIEW.v).toBeCloseTo(40.7, 1)
    expect(Math.hypot(Math.tan((AURA_VIEW.h / 2) * DEG), Math.tan((AURA_VIEW.v / 2) * DEG))).toBeCloseTo(Math.tan(35 * DEG))
  })

  it('fits the first two pages side by side, with the count, at one head pose', () => {
    const first = freePlace([], 0)!
    const second = freePlace([{ kind: 'chat', slug: 'a', slot: 'a', topic: '', ...first, stored: false }], 0)!
    const pages = [first, second].flatMap(p => corners(new Vector3(...pagePos(p.yaw, p.pitch)), FULL.width, (FULL.width * FULL_H) / FULL_W))
    // The count's surface is 0.22 m wide and 84 of 520 as tall, and stands past the desk (`scene.ts`).
    const count = corners(new Vector3(0, -0.3, -1.02), 0.22, (0.22 * 84) / 520)
    expect(poses([...pages, ...count]).length).toBeGreaterThanOrEqual(20)
  })

  it('fits every plot of the miniature when the person looks down at the desk', () => {
    const slugs = ['a', 'b', 'c', 'd', 'e', 'f']
    const map = assignPlots({}, slugs)
    const b = landBounds(map, slugs)
    const pose = landPose({ t: 0, fx: b.cx, fz: b.cz }, minScale(b.radius), 1.2)
    const plots = slugs.map(s => { const [x, z] = plotCentre(map[s]); return new Vector3(pose.x + x * pose.s, pose.y, pose.z + z * pose.s) })
    const fits = poses(plots)
    expect(fits.length).toBeGreaterThanOrEqual(20)
    // It is a look down at the desk, not level: the miniature is on the desk top.
    expect(Math.min(...fits)).toBeGreaterThan(15)
    expect(Math.abs(pose.y - DESK.y)).toBeLessThan(1e-9)
  })
})

describe('sound cues', () => {
  it('logs each cue with where it came from, even with no audio device', () => {
    const s = new XrSound()
    s.cue('ask', [0.5, -0.3, -1.2])
    s.cue('commit', [0, 0, -0.9])
    expect(s.log).toEqual([{ cue: 'ask', at: [0.5, -0.3, -1.2] }, { cue: 'commit', at: [0, 0, -0.9] }])
  })

  it('is silent until turned on', () => {
    const s = new XrSound()
    expect(s.enabled).toBe(false)
    expect(() => { s.resume(); s.listen([0, 0, 0], [0, 0, -1]) }).not.toThrow()
  })
})
