import { describe, expect, it } from 'vitest'
import { WATCH, noWatch, offLook, offLookDeg, pickWatched, type WatchPage, type WatchState } from './streamWatch'

const pg = (id: string, gazeDeg: number, inView = true): WatchPage => ({ id, gazeDeg, inView })
/** The state after the pick ran every 16 ms from `from` to `to` with these pages. */
function run(pages: readonly WatchPage[], from: WatchState, t0: number, t1: number, blocked = false): WatchState {
  let s = from
  for (let t = t0; t <= t1; t += 16) s = pickWatched(pages, s, t, blocked)
  return s
}

describe('the page he looks at, on the stream', () => {
  it('watches a page once his look has rested on it for 300 ms, and not before', () => {
    const pages = [pg('a', 3), pg('b', 35)]
    expect(run(pages, noWatch(), 0, WATCH.restMs - 20).watched).toBeNull()
    expect(run(pages, noWatch(), 0, WATCH.restMs + 20).watched).toBe('a')
  })

  it('keeps the watched page until another has been the nearest for 300 ms: a look that passes over one does not move it', () => {
    let s = run([pg('a', 3), pg('b', 40)], noWatch(), 0, 400)
    expect(s.watched).toBe('a')
    // His look crosses to b for 200 ms, then comes back: a stays watched.
    s = run([pg('a', 30), pg('b', 4)], s, 416, 616)
    expect(s.watched).toBe('a')
    s = run([pg('a', 3), pg('b', 40)], s, 632, 700)
    expect(s.watched).toBe('a')
    // His look moves to b and rests there: b is watched.
    s = run([pg('a', 30), pg('b', 4)], s, 716, 1100)
    expect(s.watched).toBe('b')
  })

  it('takes the nearer of two pages in the middle of his look', () => {
    expect(run([pg('a', 4), pg('b', 2)], noWatch(), 0, 400).watched).toBe('b')
  })

  it('on a tie keeps the watched page; with none watched, takes the one in front', () => {
    // His look meets two pages at once, one before the other: both measure 0.
    const front = { id: 'front', gazeDeg: 0, inView: true, far: 1.2 }
    const back = { id: 'back', gazeDeg: 0, inView: true, far: 1.75 }
    // None watched: the one in front, whichever was opened first.
    expect(run([back, front], noWatch(), 0, 400).watched).toBe('front')
    expect(run([front, back], noWatch(), 0, 400).watched).toBe('front')
    // The one behind is watched already (he read it, then a page came before it): it stays, for good.
    const s = run([back, pg('front', 40)], noWatch(), 0, 400)
    expect(s.watched).toBe('back')
    expect(run([back, front], s, 416, 3000).watched).toBe('back')
    expect(run([front, back], s, 416, 3000).watched).toBe('back')
    // A page that is nearer his look still takes it: a tie is only equal angles.
    expect(run([{ ...back, gazeDeg: 3 }, front], s, 416, 1000).watched).toBe('front')
    // The measure gives the distance to the page's nearest point: a page 1.2 m ahead is in front of one 1.75 m ahead.
    const page = { cx: 0, cy: 0, w: 1.56, h: 1.09 }
    const ahead = { x: 0, y: 0, z: -1 }
    expect(offLook({ x: 0, y: 0, z: 1.2 }, ahead, page)).toEqual({ deg: 0, far: 1.2 })
    expect(offLook({ x: 0, y: 0, z: 1.75 }, ahead, page).far).toBeCloseTo(1.75, 6)
  })

  it('measures exactly 0 for a look that meets the page, so a moving head still ties two pages it meets', () => {
    const page = { cx: 0.1, cy: -0.05, w: 1.56, h: 1.09 }
    // Looks that wander over the page from two places, as a head that moves a little each frame: in the page's own
    // frame, the eye off its axis and the look turned up to about 15 degrees. Each meets the page.
    const looks: { eye: { x: number; y: number; z: number }; ahead: { x: number; y: number; z: number } }[] = []
    for (let k = 0; k < 200; k += 1) {
      const yaw = ((k * 37) % 29 - 14) * (Math.PI / 180) * 0.97
      const pitch = ((k * 53) % 23 - 11) * (Math.PI / 180) * 1.03
      const eye = { x: 0.0137 * (k % 7) - 0.04, y: 0.0091 * (k % 5) - 0.02, z: k % 2 ? 1.2 : 1.75 }
      looks.push({ eye, ahead: { x: Math.sin(yaw) * Math.cos(pitch), y: Math.sin(pitch), z: -Math.cos(yaw) * Math.cos(pitch) } })
    }
    for (const { eye, ahead } of looks) expect(offLookDeg(eye, ahead, page)).toBe(0)
    // So with a look that meets both, none watched, the one in front is taken after the rest, every time.
    let s = noWatch()
    for (let k = 0; k < looks.length; k += 1) {
      const { ahead } = looks[k]
      const front = { id: 'front', inView: true, ...((o): { gazeDeg: number; far: number } => ({ gazeDeg: o.deg, far: o.far }))(offLook({ x: 0, y: 0, z: 1.2 }, ahead, page)) }
      const back = { id: 'back', inView: true, ...((o): { gazeDeg: number; far: number } => ({ gazeDeg: o.deg, far: o.far }))(offLook({ x: 0, y: 0, z: 1.75 }, ahead, page)) }
      s = pickWatched(k % 2 ? [back, front] : [front, back], s, k * 16, false)
    }
    expect(s.watched).toBe('front')
    // A look that misses the page's edge, and a page behind him, still measure more than 0.
    expect(offLookDeg({ x: 0, y: 0, z: 1.2 }, { x: Math.sin(0.8), y: 0, z: -Math.cos(0.8) }, page)).toBeGreaterThan(0)
    expect(offLookDeg({ x: 0, y: 0, z: 1.2 }, { x: 0, y: 0, z: 1 }, page)).toBeGreaterThan(90)
  })

  it('measures exactly 0 for every look that meets the page, so that two pages he looks through tie while his head moves', () => {
    const page = { cx: 0.03, cy: -0.02, w: 1.56, h: 1.09 }
    // A head that moves a little each frame, at odd places and angles: the look meets the page in every one.
    let worst = 0
    let ties = 0
    for (let i = 0; i < 2000; i += 1) {
      const yaw = (Math.sin(i * 0.137) * 9 * Math.PI) / 180
      const pitch = (Math.cos(i * 0.091) * 6 * Math.PI) / 180
      const ahead = { x: Math.sin(yaw) * Math.cos(pitch), y: Math.sin(pitch), z: -Math.cos(yaw) * Math.cos(pitch) }
      const front = offLook({ x: 0.011 * Math.sin(i), y: 0.007, z: 1.2 }, ahead, page)
      const back = offLook({ x: 0.011 * Math.sin(i), y: 0.007, z: 1.75 }, ahead, page)
      worst = Math.max(worst, front.deg, back.deg)
      if (front.deg === back.deg) ties += 1
      expect(front.far).toBeLessThan(back.far)
    }
    expect(worst).toBe(0)
    expect(ties).toBe(2000)
    // A look that misses its edge is not 0.
    expect(offLook({ x: 0, y: 0, z: 1.75 }, { x: Math.sin(0.5), y: 0, z: -Math.cos(0.5) }, page).deg).toBeGreaterThan(1)
  })

  it('watches no page far from the middle of his look', () => {
    expect(run([pg('a', WATCH.withinDeg + 5)], noWatch(), 0, 600).watched).toBeNull()
    // His look misses the page's edge by more than a hair: 5 degrees counts, 6 does not.
    expect(WATCH.withinDeg).toBe(5)
    expect(run([pg('a', 5)], noWatch(), 0, 600).watched).toBe('a')
    expect(run([pg('a', 6)], noWatch(), 0, 600).watched).toBeNull()
  })

  it('ends the watch when no page has been near his look for 1.5 s, and not for a glance away', () => {
    const s = run([pg('a', 3)], noWatch(), 0, 400)
    expect(s.watched).toBe('a')
    // He looks at the Bots beside the chat: his look misses its edge by 8 degrees, and it is still in view.
    const away = [pg('a', 8)]
    expect(run(away, s, 416, 416 + WATCH.endMs - 100).watched).toBe('a')
    expect(run(away, s, 416, 416 + WATCH.endMs + 50).watched).toBeNull()
    // A glance away of a second and back: the watch never ended, and the time away starts anew.
    let t = run(away, s, 416, 1400)
    t = run([pg('a', 3)], t, 1416, 1500)
    expect(t.watched).toBe('a')
    expect(run(away, t, 1516, 1516 + WATCH.endMs - 100).watched).toBe('a')
    // After the end his look comes back: the page is watched again after its rest.
    const ended = run(away, s, 416, 416 + WATCH.endMs + 50)
    expect(run([pg('a', 3)], ended, 2000, 2000 + WATCH.restMs + 20).watched).toBe('a')
  })

  it('measures a wide page by its point nearest his look: he reads the right third of a 2 m page', () => {
    // The page is 2 m wide and 1 m high, 1.9 m ahead of him (he stands at z = 1.9 in its frame and looks along -z).
    const page = { cx: 0, cy: 0, w: 2, h: 1 }
    const eye = { x: 0, y: 0, z: 1.9 }
    const look = (yawDeg: number, pitchDeg = 0) => {
      const y = (yawDeg * Math.PI) / 180
      const p = (pitchDeg * Math.PI) / 180
      return { x: Math.sin(y) * Math.cos(p), y: Math.sin(p), z: -Math.cos(y) * Math.cos(p) }
    }
    expect(offLookDeg(eye, look(0), page)).toBeCloseTo(0, 5)
    // 22 degrees to the right: 0.77 m from its middle, inside its right third. Its middle is 22 degrees away.
    expect(offLookDeg(eye, look(22), page)).toBeCloseTo(0, 5)
    // Past its right edge (27.8 degrees): the angle to that edge, not to its middle.
    expect(offLookDeg(eye, look(40), page)).toBeCloseTo(40 - (Math.atan2(1, 1.9) * 180) / Math.PI, 3)
    // Over its top edge: the angle to that edge.
    expect(offLookDeg(eye, look(0, 30), page)).toBeCloseTo(30 - (Math.atan2(0.5, 1.9) * 180) / Math.PI, 3)
    // A look away from the page's plane is measured to its middle.
    expect(offLookDeg(eye, look(180), page)).toBeCloseTo(180, 3)
    // So the pick takes the wide page he reads at its side over a page whose middle is nearer.
    const wide = pg('wide', offLookDeg(eye, look(22), page))
    expect(run([wide, pg('other', 3)], noWatch(), 0, 400).watched).toBe('wide')
    // Past its edge by more than 5 degrees it does not count: 27.8 + 6.
    expect(run([pg('wide', offLookDeg(eye, look(33.8), page))], noWatch(), 0, 600).watched).toBeNull()
    expect(run([pg('wide', offLookDeg(eye, look(31.8), page))], noWatch(), 0, 600).watched).toBe('wide')
  })

  it('ends the watch with three pages of the ring round him when his look is level between two of them', () => {
    // Pages of the default size (1.56 m by 1.09 m) at 1.75 m, at -60, 0 and 60 degrees, each turned to him.
    const page = { cx: 0, cy: 0, w: 1.56, h: 1.09 }
    const eye = { x: 0, y: 0, z: 1.75 }
    const ring = [-60, 0, 60]
    // His look at `yawDeg`, as each page of the ring measures it in its own frame.
    const at = (yawDeg: number): WatchPage[] =>
      ring.map(r => {
        const y = ((yawDeg - r) * Math.PI) / 180
        return pg(`p${r}`, offLookDeg(eye, { x: Math.sin(y), y: 0, z: -Math.cos(y) }, page))
      })
    // He reads the middle page: watched.
    const s = run(at(0), noWatch(), 0, 400)
    expect(s.watched).toBe('p0')
    // He looks level between the middle page and the right one (their edges are 24 degrees from their middles).
    const between = at(30)
    expect(between.map(p => Math.round(p.gazeDeg))).toEqual([90, 6, 6])
    expect(run(between, s, 416, 416 + WATCH.endMs - 100).watched).toBe('p0')
    expect(run(between, s, 416, 416 + WATCH.endMs + 50).watched).toBeNull()
    // And on to the right page: it is watched after its rest.
    expect(run(at(60), run(between, s, 416, 2000), 2016, 2016 + WATCH.restMs + 20).watched).toBe('p60')
  })

  it('drops a watched page that leaves his view or closes', () => {
    const s = run([pg('a', 3), pg('b', 40)], noWatch(), 0, 400)
    expect(pickWatched([pg('a', 3, false), pg('b', 40)], s, 416, false).watched).toBeNull()
    expect(pickWatched([pg('b', 40)], s, 416, false).watched).toBeNull()
  })

  it('watches no page while his hands are busy or the pages are put away, and starts again after', () => {
    let s = run([pg('a', 3)], noWatch(), 0, 400)
    s = pickWatched([pg('a', 3)], s, 416, true)
    expect(s.watched).toBeNull()
    expect(run([pg('a', 3)], s, 432, 600, true).watched).toBeNull()
    expect(run([pg('a', 3)], s, 616, 1000).watched).toBe('a')
  })
})
