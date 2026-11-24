import { describe, expect, it } from 'vitest'
import { M_PER_PX, PAGES } from './layout'
import { LIFE, crossesPage, idleAct, inQuiet, quietFor, readsPage, visitHolds, visitSpot, walkAt, walkMs, walkWay, wayLength, wayPoint, type Way } from './placeLife'
import { HOME_STRIP, PLACES, SPOTS, deskAt, facesClash, forwardCovers, inView, onBox, onDesk, seenBox, spotOf, spotPoint, stage, placeHomes, type Pt } from './places'

const dist = (a: readonly number[], b: readonly number[]) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2])
const firstPage = seenBox({ x: 0, y: PAGES.eyeM, z: -PAGES.distanceM }, PAGES.cssWidth * M_PER_PX, PAGES.cssHeight * M_PER_PX)
const none = { bodies: [], pages: [], quiet: null }
const front = spotPoint(PLACES.forward.yawDeg, PLACES.forward.pitchDeg, PLACES.forward.distanceM)
const samples = (way: Way) => Array.from({ length: 61 }, (_, k) => wayPoint(way, k / 60))

describe('a walk between two spots', () => {
  it('goes straight where nothing is in the way, a second for a metre, and never shorter than its least', () => {
    const way = walkWay(spotOf(0), front, none) as Way
    expect(way.via).toBeNull()
    expect(walkMs(way)).toBeCloseTo(dist(spotOf(0), front) * 1000, 0)
    expect(walkMs({ from: [0, 1, -2], to: [0.1, 1, -2], via: null })).toBe(LIFE.leastMs)
  })
  it('eases in and out, starts at its start, ends at its end, and heads the way it goes', () => {
    const way: Way = { from: [0, 1, -2], to: [1, 1, -2], via: null }
    expect(walkAt(way, 0).at).toEqual([0, 1, -2])
    expect(walkAt(way, 1).at).toEqual([1, 1, -2])
    expect(walkAt(way, 0.5).at[0]).toBeCloseTo(0.5, 6)
    // Slow at both ends: a tenth of the time moves it less than a tenth of the way.
    expect(walkAt(way, 0.1).at[0]).toBeLessThan(0.05)
    expect(walkAt(way, 0.9).at[0]).toBeGreaterThan(0.95)
    // It walks to the right: its yaw is a quarter turn from facing him.
    expect(walkAt(way, 0.5).yaw).toBeCloseTo(Math.PI / 2, 6)
  })
  it('passes around a body that stands in its way, and through nobody', () => {
    const [from, to]: [Pt, Pt] = [[-0.6, 1.2, -2], [0.6, 1.2, -2]]
    const body: Pt = [0, 1.2, -2]
    const way = walkWay(from, to, { ...none, bodies: [body] }) as Way
    expect(way.via).not.toBeNull()
    for (const p of samples(way)) expect(dist(p, body)).toBeGreaterThanOrEqual(LIFE.clearM - 0.01)
    // A bent way is longer, and takes longer.
    expect(wayLength(way)).toBeGreaterThan(1.2)
    expect(walkMs(way)).toBeGreaterThan(1200)
  })
  it('takes the one that needs him past every face as he sees them, not only past the bodies', () => {
    // A Bot stands a little to the side of the way, far enough for the bodies, but in front of it he sees the walker cover its face.
    const [from, to]: [Pt, Pt] = [[0, 1.2, -3.2], [0, 1.2, -1]]
    const body: Pt = [0.3, 1.25, -2.6]
    const plain = walkWay(from, to, { ...none, bodies: [body] }) as Way
    expect(plain.via).toBeNull()
    expect(samples(plain).some(p => dist(p, from) > LIFE.clearM && facesClash(p, body, 0))).toBe(true)
    const asker = walkWay(from, to, { ...none, bodies: [body], faces: true }) as Way
    expect(asker.via).not.toBeNull()
    for (const p of samples(asker)) if (dist(p, from) >= LIFE.clearM && dist(p, to) >= LIFE.clearM) { expect(facesClash(p, body, 0)).toBe(false); expect(dist(p, body)).toBeGreaterThanOrEqual(LIFE.clearM - 0.01) }
    // Walled in by faces on every side, it still walks: the nearest way that keeps clear of the bodies.
    const wall: Pt[] = [-0.9, -0.6, -0.3, 0, 0.3, 0.6, 0.9].flatMap(x => [0.2, 0.7, 1.2, 1.7, 2.2].map(y => [x, y, -2.6] as Pt)).filter(p => dist(p, [0, 1.2, -2.6]) > 0.28)
    expect(walkWay(from, to, { ...none, bodies: wall, faces: true })).not.toBeNull()
  })
  it('never crosses a page he reads: around it where a way is free, else no walk at all', () => {
    // From the left of the first page to its right: no bend clears a page of that size.
    const [left, right]: [Pt, Pt] = [[-1.3, 1.5, -1.9], [1.3, 1.5, -1.9]]
    expect(walkWay(left, right, { ...none, pages: [firstPage] })).toBeNull()
    // A small page between two spots: the way bends around it, and no point of it lies on the page.
    const small = seenBox({ x: 0, y: 1.6, z: -2 }, 0.3, 0.3)
    const way = walkWay([-0.5, 1.45, -2], [0.5, 1.45, -2], { ...none, pages: [small] }) as Way
    expect(way).not.toBeNull()
    for (const p of samples(way)) expect(onBox(p, small, 0)).toBe(false)
    // From the front, home, with the first page open: the front is on the page, so it does not walk.
    expect(walkWay(front, spotOf(0), { ...none, pages: [firstPage] })).toBeNull()
  })
  it('finds a page that opened on the rest of a walk, and only on the rest of it', () => {
    const way: Way = { from: [-0.5, 1.45, -2], to: [0.5, 1.45, -2], via: null }
    const small = seenBox({ x: 0, y: 1.6, z: -2 }, 0.3, 0.3)
    expect(crossesPage(way, 0, [small])).toBe(true)
    expect(crossesPage(way, 0, [])).toBe(false)
    // Past the page already: the rest of the way is clear of it.
    expect(crossesPage(way, 0.8, [small])).toBe(false)
    // Planned again from where it stands, the way goes round the page.
    const round = walkWay(walkAt(way, 0.2).at, way.to, { ...none, pages: [small] }) as Way
    expect(round).not.toBeNull()
    expect(crossesPage(round, 0, [small])).toBe(false)
  })
  it('leaves the middle of his look alone while he reads a page or holds a board', () => {
    const look = { yaw: 0, pitch: 0 }
    expect(inQuiet(spotOf(2), look)).toBe(true)
    expect(inQuiet(spotOf(0), look)).toBe(false)
    // A walk that would cross the middle finds no way, or one that stays out of it.
    const [a, b]: [Pt, Pt] = [spotOf(3), spotOf(4)]
    const way = walkWay(a, b, { ...none, quiet: look })
    if (way) for (const p of samples(way)) expect(inQuiet(p, look)).toBe(false)
    // To the front, in the middle: no walk.
    expect(walkWay(spotOf(0), front, { ...none, quiet: look })).toBeNull()
    // He looks elsewhere: the same walk is free.
    expect(walkWay(spotOf(0), front, { ...none, quiet: { yaw: 60, pitch: 0 } })).not.toBeNull()
  })
})

describe('the one that needs him, while a page is open', () => {
  const side = (yaw: number) => seenBox({ x: Math.sin((yaw * Math.PI) / 180) * 1.75, y: PAGES.eyeM, z: -Math.cos((yaw * Math.PI) / 180) * 1.75 }, 0.8, 0.6)
  it('reads a page only when the middle of his look is on it', () => {
    expect(readsPage({ yaw: 0, pitch: 0 }, [firstPage])).toBe(true)
    expect(readsPage({ yaw: 0, pitch: 0 }, [side(55)])).toBe(false)
    expect(readsPage({ yaw: 55, pitch: 0 }, [side(55)])).toBe(true)
    expect(readsPage({ yaw: 55, pitch: 30 }, [side(55)])).toBe(false)
    expect(readsPage({ yaw: 0, pitch: 0 }, [])).toBe(false)
  })
  it('reaches him: a page at a side, he looks ahead, each of the seven asks in turn (28 of 28)', () => {
    const slugs = Array.from({ length: 7 }, (_, i) => `b${i}`)
    const homes = placeHomes(slugs)
    let reached = 0
    for (const yaw of [-70, -55, 55, 70]) for (let i = 0; i < 7; i += 1) {
      const pages = [side(yaw)]
      const look = { yaw: 0, pitch: 0 }
      const quiet = readsPage(look, pages) ? look : null
      expect(quiet).toBeNull()
      const s = stage({ bots: slugs.map((slug, k) => ({ slug, state: k === i ? 'needs' : 'idle' })), homes, pages, block: null, known: true, homeLarge: false })
      expect(s[i].forward).toBe(true)
      const way = walkWay(homes[i], s[i].at, { bodies: s.filter((_, k) => k !== i).map(x => x.at), pages, quiet: quietFor(quiet, true, false), faces: true })
      if (way) reached += 1
    }
    expect(reached).toBe(28)
  })
  it('is not held by the quiet middle, even while he holds a board or reads a page; nor is a walk under way', () => {
    const look = { yaw: 0, pitch: 0 }
    // The front spot is inside the quiet middle: a walk that kept out of it would never start.
    expect(inQuiet(front, look)).toBe(true)
    expect(walkWay(spotOf(0), front, { ...none, quiet: look })).toBeNull()
    expect(walkWay(spotOf(0), front, { ...none, quiet: quietFor(look, true, false) })).not.toBeNull()
    // Its ask ends on the way: it walks on home from where it is, across the middle if it must.
    const between: Pt = [0.1, 1.3, -1.3]
    expect(walkWay(between, spotOf(1), { ...none, quiet: quietFor(look, false, true) })).not.toBeNull()
    // An idle Bot that would start a walk across the middle is still held.
    expect(quietFor(look, false, false)).toBe(look)
  })
})

describe('a spot or a way, against the room as it is now', () => {
  const SEVEN = ['working', 'idle', 'working', 'idle', 'working', 'failed', 'idle']
  const slugs = (n: number) => Array.from({ length: n }, (_, i) => `b${i}`)
  it('ends a visit at once when an ask comes over it: the reviewer\'s 72 pairs, none left under the board or at the face', () => {
    const homes = placeHomes(slugs(SEVEN.length))
    let pairs = 0
    let under = 0
    let ended = 0
    for (let v = 0; v < SEVEN.length; v += 1) {
      if (SEVEN[v] !== 'idle') continue
      // Picked with nobody in front.
      const spot = visitSpot(v, { at: homes, open: homes.map((_, j) => j !== v), desks: [], boxes: [HOME_STRIP] })
      if (!spot) continue
      for (let k = 0; k < SEVEN.length; k += 1) {
        if (k === v) continue
        for (const lookYaw of [0, 15, -15, 28]) {
          const s = stage({ bots: SEVEN.map((st, i) => ({ slug: `b${i}`, state: i === k ? 'needs' : st })), homes, pages: [], block: null, known: true, homeLarge: false, lookYaw })
          if (!s[k].forward) continue
          pairs += 1
          const box = forwardCovers(s[k].at, { yaw: lookYaw, pitch: 0 })
          const hold = { covers: [HOME_STRIP, ...(box ? [box] : [])], faces: [s[k].at] }
          // Where the visitor stands now: its visit if that holds, else the spot that the rule of places gives it.
          const keeps = visitHolds(spot, hold)
          if (!keeps) ended += 1
          const at = keeps ? spot : s[v].at
          if ((box && onBox(at, box)) || facesClash(s[k].at, at)) under += 1
        }
      }
    }
    expect(pairs).toBe(72)
    // 30 since Home's strip is read where it stands (C2, D80 1: higher than the old line to a level page's top), so
    // fewer visits stand where the strip was; none is left under the board or at the face (`under`, below).
    expect(ended).toBe(30)
    expect(under).toBe(0)
  })
  it('keeps the 0.27 m at the way\'s end too, and past where the others are going', () => {
    const [from, to]: [Pt, Pt] = [[-0.6, 1.2, -2], [0.6, 1.2, -2]]
    // A body that stands 0.2 m from the end: no way ends there.
    expect(walkWay(from, to, { ...none, bodies: [[0.6, 1.2, -1.8]] })).toBeNull()
    // A body that is on its way across the middle: its points count as bodies, so the way bends past them.
    const crossing: Pt[] = [[0, 1.2, -2.4], [0, 1.2, -2.1], [0, 1.2, -1.8]]
    const way = walkWay(from, to, { ...none, bodies: crossing }) as Way
    expect(way).not.toBeNull()
    for (const p of samples(way)) for (const b of crossing) if (dist(p, from) >= LIFE.clearM) expect(dist(p, b)).toBeGreaterThanOrEqual(LIFE.clearM - 0.01)
  })
})

describe('an idle Bot\'s life', () => {
  it('gives each Bot beats of its own: no two pick in step, and each act comes', () => {
    const beats = (slug: string) => Array.from({ length: 200 }, (_, k) => idleAct(slug, k * LIFE.beatMs).act)
    const [a, b] = [beats('atlas'), beats('otto')]
    expect(a).not.toEqual(b)
    for (const act of ['rest', 'look', 'stretch', 'nap', 'visit'] as const) expect(a.filter(x => x === act).length).toBeGreaterThan(0)
    // Calm: most of the time it rests, looks or naps; a visit is rare.
    expect(a.filter(x => x === 'visit').length).toBeLessThan(40)
    // Within a beat the act holds, and the beat's own time runs.
    const one = idleAct('atlas', 100_000)
    const later = idleAct('atlas', 100_000 + 500)
    if (later.beat === one.beat) { expect(later.act).toBe(one.act); expect(later.ranMs - one.ranMs).toBeCloseTo(500, 6) }
    expect(idleAct('atlas', 0).beat).not.toBe(idleAct('otto', LIFE.beatMs * 3).beat)
  })
  it('visits a neighbour a few steps away: beside it, over no face, on no desk, in view', () => {
    const homes = placeHomes(Array.from({ length: 7 }, (_, i) => `b${i}`))
    const desks = homes.map(deskAt)
    const ask = { at: homes, open: homes.map(() => true), desks, boxes: [] }
    let visits = 0
    homes.forEach((home, i) => {
      const spot = visitSpot(i, ask)
      if (!spot) return
      visits += 1
      expect(dist(spot, home)).toBeLessThanOrEqual(LIFE.visit.mostM + LIFE.visit.besideM)
      expect(inView(spot)).toBe(true)
      homes.forEach((o, k) => { if (k !== i) { expect(facesClash(o, spot, 0)).toBe(false); expect(dist(o, spot)).toBeGreaterThanOrEqual(LIFE.clearM) } })
      for (const desk of desks) expect(onDesk(spot, desk)).toBe(false)
      // It stands beside somebody.
      expect(Math.min(...homes.filter((_, k) => k !== i).map(o => dist(o, spot)))).toBeLessThan(0.4)
    })
    // In the list of seven some Bot has a neighbour near enough.
    expect(visits).toBeGreaterThan(0)
    expect(SPOTS.length).toBe(7)
  })
  it('visits nobody that may not be visited, stands on no page, and stays when no neighbour is near', () => {
    const homes = placeHomes(Array.from({ length: 7 }, (_, i) => `b${i}`))
    const ask = { at: homes, open: homes.map(() => true), desks: homes.map(deskAt), boxes: [] }
    const i = homes.findIndex((_, k) => visitSpot(k, ask))
    const spot = visitSpot(i, ask) as Pt
    const j = homes.findIndex((o, k) => k !== i && dist(o, spot) < 0.4)
    // The one in front, or one that walks, is not open: another neighbour, or nobody.
    const shut = visitSpot(i, { ...ask, open: homes.map((_, k) => k !== j) })
    if (shut) expect(dist(shut, homes[j])).toBeGreaterThan(0.4)
    // A page that covers the spot: not there.
    const page = seenBox({ x: spot[0], y: spot[1] + 0.15, z: spot[2] }, 0.5, 0.5)
    const off = visitSpot(i, { ...ask, boxes: [page] })
    if (off) expect(onBox(off, page, 0)).toBe(false)
    // Alone in the room: no visit.
    expect(visitSpot(0, { at: [homes[0]], open: [true], desks: [], boxes: [] })).toBeNull()
    // The rule of where each Bot is does not know of visits: an idle one is at its place.
    expect(stage({ bots: [{ slug: 'b0', state: 'idle' }], homes: [homes[0]], pages: [], block: null, known: true, homeLarge: false })[0].at).toEqual(homes[0])
  })
})
