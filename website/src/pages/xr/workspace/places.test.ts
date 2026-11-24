import { describe, expect, it } from 'vitest'
import { HOME, homeLayout } from './home'
import { FORWARD_BOX, HOME_BOX, HOME_STRIP, PLACES, SPOTS, WITH_BOARD, deskAt, facesClash, frontNow, frontOf, inView, keptPoint, leansNow, onBox, onSphere, placeHomes, placeInView, reachOf, rectOf, seenAt, seenBox, spotOf, stage, yawOf, type Pt, type StageAsk , pageSignCount, placesWanted, staysHome, heldHome } from './places'
import { M_PER_PX, PAGES } from './layout'

const slugs = (n: number) => Array.from({ length: n }, (_, i) => `b${i}`)
const ask = (states: string[], over: Partial<StageAsk> = {}): StageAsk => ({
  bots: states.map((state, i) => ({ slug: `b${i}`, state })),
  homes: placeHomes(slugs(states.length)),
  pages: [], block: null, known: true, homeLarge: false, ...over,
})
/** The first page where it opens: straight ahead, level, at its own distance. */
const firstPage = seenBox({ x: 0, y: PAGES.eyeM, z: -PAGES.distanceM }, PAGES.cssWidth * M_PER_PX, PAGES.cssHeight * M_PER_PX)
const noClash = (pts: readonly Pt[]) => {
  for (let i = 0; i < pts.length; i += 1) for (let j = i + 1; j < pts.length; j += 1) if (facesClash(pts[i], pts[j], 0)) return `${i} and ${j}`
  return ''
}
const SEVEN = ['working', 'idle', 'working', 'idle', 'working', 'failed', 'idle']
const TWELVE = [...SEVEN, 'working', 'idle', 'working', 'idle', 'working']
const EVERY = ['working', 'idle', 'failed']

describe('what he sees, as a camera has it', () => {
  it('puts a point by its tangents from the look\'s middle, and a body is cut at a corner before a box of yaw and pitch says so', () => {
    expect(seenAt([0, PAGES.eyeM, -2])).toEqual([0, 0])
    const [x, y] = seenAt([1, PAGES.eyeM + 1, -2]) as [number, number]
    expect(x).toBeCloseTo(0.5, 6)
    expect(y).toBeCloseTo(0.5, 6)
    expect(seenAt([0, PAGES.eyeM, 2])).toBeNull()
    // A point 27 degrees to the side and 18.5 up (as yaw and pitch) is above a view that is 20.5 degrees high.
    const d = 3
    const corner: Pt = [Math.sin((27 * Math.PI) / 180) * d, PAGES.eyeM + d * Math.tan((18.5 * Math.PI) / 180), -Math.cos((27 * Math.PI) / 180) * d]
    expect((seenAt(corner) as [number, number])[1]).toBeGreaterThan(Math.tan((20.5 * Math.PI) / 180))
  })
})

describe('which room opens', () => {
  it('is the room of places with no word, and the row with `places=0` or with a desk', () => {
    expect(placesWanted(null, false)).toBe(true)
    expect(placesWanted('1', false)).toBe(true)
    expect(placesWanted('', false)).toBe(true)
    expect(placesWanted('0', false)).toBe(false)
    // With a desk the crew sits at its seats, whatever the word.
    expect(placesWanted(null, true)).toBe(false)
    expect(placesWanted('1', true)).toBe(false)
  })
})

describe('the list of places', () => {
  const first = SPOTS.map((_, i) => spotOf(i))
  it('has every place whole in a level view with its desk and its sign and two degrees of air', () => {
    for (const p of first) expect(placeInView(p)).toBe(true)
    // The margin is real: the same test with no air still holds, and one pushed 4 degrees out does not.
    const out = spotOf(5)
    const pushed: Pt = [out[0] * 1.2, out[1], out[2]]
    expect(placeInView(pushed)).toBe(false)
    // Every place, the ring's too, is where a put-down may stand: a Bot that he lifts and lets go does not come nearer.
    for (let i = 0; i < 30; i += 1) {
      expect(reachOf(spotOf(i))).toBeLessThanOrEqual(PLACES.farM)
      expect(reachOf(spotOf(i))).toBeGreaterThanOrEqual(PLACES.nearM)
      expect(keptPoint(spotOf(i)).map((v, k) => Math.abs(v - spotOf(i)[k]) < 0.006)).toEqual([true, true, true])
    }
  })
  it('keeps Home\'s strip and the low middle clear, and no body over another one\'s face', () => {
    for (const p of first) expect(onBox(p, HOME_STRIP)).toBe(false)
    // The two nearest stand left and right of the low middle, where the one that needs him comes.
    expect(onBox(first[0], FORWARD_BOX, 0)).toBe(false)
    expect(onBox(first[1], FORWARD_BOX, 0)).toBe(false)
    expect(noClash(first)).toBe('')
  })
  it('holds the middle band: the nearest left and right of the middle, far ones behind and between them', () => {
    const d = first.map(p => Math.hypot(p[0], p[2]))
    expect(yawOf(first[0])).toBeLessThan(-12)
    expect(yawOf(first[1])).toBeGreaterThan(12)
    expect(Math.max(d[0], d[1])).toBeLessThanOrEqual(2)
    const between = first.filter(p => Math.abs(yawOf(p)) < 10)
    expect(between.length).toBe(1)
    for (const p of between) expect(Math.hypot(p[0], p[2])).toBeGreaterThan(2.5)
    // Never a row: no three at one height.
    const mids = SPOTS.map(s => Math.round(s[1] / 3))
    for (const m of mids) expect(mids.filter(x => x === m).length).toBeLessThan(3)
  })
  it('stands a larger crew in a ring to the sides, whole outside the level view, with no body over a face', () => {
    const all = Array.from({ length: 30 }, (_, i) => spotOf(i))
    const edge = Math.tan((PLACES.viewHalfYaw * Math.PI) / 180)
    for (const p of all.slice(SPOTS.length)) {
      const r = rectOf(p, { yaw: 0, pitch: 0 }, { left: 0.45, right: 0.45, top: 0.4, bottom: 0.05 })
      // Never half in: with its desk and an idle step to either side it is past the view's edge, or behind him.
      expect(r === null || r.x0 > edge || r.x1 < -edge).toBe(true)
    }
    expect(noClash(all)).toBe('')
  })
  it('keeps a kept point, and gives an old row yaw the free place nearest it', () => {
    const kept: Pt = [0.5, 1.2, -2]
    const homes = placeHomes(['a', 'b', 'c'], { b: kept }, { c: 23 })
    expect(homes[1]).toEqual(kept)
    expect(Math.abs(yawOf(homes[2]) - 23)).toBeLessThan(1.5)
    expect(homes[0]).toEqual(spotOf(0))
    // A Bot that he put down leaves its place empty: nobody after it in the crew moves up.
    const seven = placeHomes(slugs(7), { b2: kept })
    seven.forEach((p, i) => expect(p).toEqual(i === 2 ? kept : spotOf(i)))
  })
  it('opens an old room that kept only the row: every Bot at a place of its own, whole in the view', () => {
    const rows: Record<string, number>[] = [
      Object.fromEntries(slugs(7).map((s, i) => [s, -30 + i * 10])),
      Object.fromEntries(slugs(7).map(s => [s, 4])),
      { b0: 150, b3: -170, b5: 150 },
      { b6: -22, b1: -22 },
    ]
    for (const yaws of rows) {
      const homes = placeHomes(slugs(7), {}, yaws)
      expect(new Set(homes.map(h => h.join())).size).toBe(7)
      for (const h of homes) expect(placeInView(h)).toBe(true)
      expect(noClash(homes)).toBe('')
    }
    // A larger crew: the ring takes the rest as it does with no record, and still no two share a place.
    const twelve = placeHomes(slugs(12), {}, Object.fromEntries(slugs(12).map((s, i) => [s, -55 + i * 10])))
    expect(new Set(twelve.map(h => h.join())).size).toBe(12)
    expect(noClash(twelve)).toBe('')
    expect(twelve.filter(h => placeInView(h)).length).toBe(SPOTS.length)
  })
})

describe('where a Bot is says what it does', () => {
  it('stands a working one at its desk, turned to it, its desk lit; a failed one at its desk, facing him, its desk dark', () => {
    const s = stage(ask(SEVEN))
    expect(s[0].does).toBe('work')
    expect(s[0].at).toEqual(spotOf(0))
    expect(s[0].face).not.toBeNull()
    expect(s[0].desk.lit).toBe(true)
    expect(s[5].does).toBe('failed')
    expect(s[5].at).toEqual(spotOf(5))
    expect(s[5].face).toBeNull()
    expect(s[5].desk.lit).toBe(false)
    // An idle one's desk is dark too.
    expect(s[1].desk.lit).toBe(false)
  })
  it('stands an idle one right beside its desk, turned a little away from it: no desk stands alone', () => {
    const cases = [SEVEN, TWELVE, Array(7).fill('idle'), ...EVERY.map(st => Array.from({ length: 7 }, (_, i) => (i % 2 ? 'idle' : st)))]
    for (const states of cases) {
      const a = ask(states)
      const s = stage(a)
      s.forEach((x, i) => {
        if (x.does !== 'idle') return
        // Touching distance: at its place, at most 0.25 m from its desk, in view with it.
        expect(x.at).toEqual(a.homes[i])
        expect(Math.hypot(x.at[0] - x.desk.at[0], x.at[2] - x.desk.at[2])).toBeLessThanOrEqual(0.25)
        if (i < SPOTS.length) expect(placeInView(x.at)).toBe(true)
        // Turned away from the desk: it faces farther from the desk than a look at him does.
        const toDesk = Math.atan2(x.desk.at[0] - x.at[0], x.desk.at[2] - x.at[2])
        const off = (f: number) => Math.abs(((f - toDesk + 3 * Math.PI) % (2 * Math.PI)) - Math.PI)
        expect(x.face).not.toBeNull()
        expect(off(x.face as number)).toBeGreaterThan(off(Math.atan2(-x.at[0], -x.at[2])) + 0.3)
        expect(x.desk.lit).toBe(false)
      })
      expect(noClash(s.map(x => x.at))).toBe('')
    }
  })
  it('brings only the first one that needs him forward, nearest of all; the others stay at their desks', () => {
    for (const block of [null, 'tray', 'keys'] as const) {
      const a = ask(['working', 'needs', 'idle', 'needs', 'idle', 'needs', 'working'], { block })
      const s = stage(a)
      const d = (p: Pt) => Math.hypot(p[0], p[2])
      expect(s.map(x => x.forward)).toEqual([false, true, false, false, false, false, false])
      if (!block) {
        expect(d(s[1].at)).toBeCloseTo(PLACES.forward.distanceM, 2)
        expect(Math.abs(yawOf(s[1].at))).toBeLessThan(5)
      }
      expect(s.every((x, i) => i === 1 || d(x.at) > d(s[1].at))).toBe(true)
      // No queue: the next ones are at their places, whole in view, and still say that they need him.
      for (const i of [3, 5]) {
        expect(s[i].does).toBe('needs')
        expect(s[i].at).toEqual(a.homes[i])
        expect(placeInView(s[i].at)).toBe(true)
      }
      // Nobody is cut by the view's edge, and the one in front covers nobody's face.
      for (const x of s) expect(inView(x.at)).toBe(true)
      expect(noClash(s.map(x => x.at))).toBe('')
    }
  })
  it('keeps the one in front there until its own ask ends: a second Bot that starts to ask does not take the place', () => {
    const bot = (slug: string, state: string, ...ts: number[]) => ({ slug, state, needs: ts.map(t => ({ ts: t })) })
    // Juno (b1) stands in front. Atlas (b0), earlier in the crew's order, starts to ask.
    const both = [bot('b0', 'needs', 50), bot('b1', 'needs', 100), bot('b2', 'working')]
    expect(frontOf(both, 'b1')).toBe('b1')
    const s = stage(ask([], { bots: both, homes: placeHomes(slugs(3)), front: frontOf(both, 'b1') }))
    expect(s.map(x => x.forward)).toEqual([false, true, false])
    expect(s[0].at).toEqual(spotOf(0))
    // Its ask ends: the next is the Bot whose oldest ask is oldest, not the first in the crew's order.
    const next = [bot('b0', 'needs', 300, 200), bot('b1', 'idle'), bot('b2', 'needs', 400, 150)]
    expect(frontOf(next, 'b1')).toBe('b2')
    // With nobody in front and no times told: the crew's order. Nobody asks: nobody.
    expect(frontOf([bot('b0', 'idle'), bot('b1', 'needs'), bot('b2', 'needs')], '')).toBe('b1')
    expect(frontOf([bot('b0', 'idle')], 'b0')).toBe('')
  })
  it('moves nothing while the states cannot be known: the one in front keeps its place and its look, a put Bot its point', () => {
    const bot = (slug: string, state: string) => ({ slug, state })
    const asking = [bot('b0', 'working'), bot('b1', 'needs'), bot('b2', 'idle')]
    const looks = [40, 0]
    const came = frontNow({ slug: '', yaw: 0 }, asking, true, undefined, () => looks.shift() as number)
    expect(came).toEqual({ slug: 'b1', yaw: 40 })
    // The socket drops: every state reads as stale, and he looks elsewhere. Nothing is chosen anew.
    const stale = [bot('b0', 'idle'), bot('b1', 'idle'), bot('b2', 'idle')]
    const gap = frontNow(came, stale, false, undefined, () => looks.shift() as number)
    expect(gap).toBe(came)
    // In the gap the one in front keeps its spot, where he looked when it came; every other Bot stands at its place,
    // a Bot that he put down at its kept point. Its spot is the same as before the gap.
    const put: Pt = [0.5, 1.2, -2]
    const homes = placeHomes(slugs(3), { b2: put })
    const before = stage(ask([], { bots: asking, homes, front: came.slug, lookYaw: came.yaw, put: new Set() }))
    const during = stage(ask([], { bots: stale, homes, known: false, front: gap.slug, lookYaw: gap.yaw, put: new Set() }))
    expect(during[1].forward).toBe(true)
    expect(during[1].at).toEqual(before[1].at)
    expect([during[0].at, during[2].at]).toEqual([homes[0], homes[2]])
    expect(during.some((x, i) => i !== 1 && (x.forward || x.moved))).toBe(false)
    // The states come back: it is in front still, at the look it came to; the look was not read again.
    const back = frontNow(gap, asking, true, undefined, () => looks.shift() as number)
    expect(back).toBe(came)
    expect(looks).toEqual([0])
    const after = stage(ask([], { bots: asking, homes, front: back.slug, lookYaw: back.yaw, put: new Set(['b2']) }))
    expect(after[1].forward).toBe(true)
    expect(yawOf(after[1].at)).toBeCloseTo(40 + PLACES.forward.yawDeg, 1)
    expect(after[2].at).toEqual(put)
  })
  it('lets the next one come forward when he put the one in front down', () => {
    const a = ask(['needs', 'working', 'needs'], { put: new Set(['b0']) })
    expect(frontOf(a.bots, 'b0', a.put)).toBe('b2')
    const s = stage(a)
    expect(s.map(x => x.forward)).toEqual([false, false, true])
    expect(s[0].at).toEqual(a.homes[0])
  })
  it('sends the one in front home when he hands its board back, and the next one comes', () => {
    const bot = (slug: string, state: string, ...asks: string[]) => ({ slug, state, needs: asks.map((id, i) => ({ id, ts: i, kind: id.startsWith('q') ? 'question' : 'approval' })) })
    const bots = [bot('b0', 'needs', 'a0'), bot('b1', 'needs', 'a1'), bot('b2', 'idle')]
    const held = (home: Record<string, string>, crew = bots) => new Set(heldHome(home, crew))
    expect(frontOf(bots, 'b0', held(staysHome({}, {}, bots)))).toBe('b0')
    const home = staysHome({}, { b0: 'a0' }, bots)
    expect(frontOf(bots, 'b0', held(home))).toBe('b1')
    const s = stage(ask([], { bots, homes: placeHomes(slugs(3)), front: 'b1' }))
    expect(s.map(x => x.forward)).toEqual([false, true, false])
    expect(s[0].at).toEqual(placeHomes(slugs(3))[0])
    // He takes that board again: it stays home, and the same record is given so nothing is drawn anew.
    expect(staysHome(home, {}, bots)).toBe(home)
    const alone = [bot('b0', 'needs', 'a0'), bot('b1', 'idle')]
    expect(frontOf(alone, 'b1', held(home, alone))).toBe('')
    // Only that ask holds it: with a question past the handed-back approval it comes forward as any asker.
    const asksMore = [bot('b0', 'needs', 'a0', 'q0'), bot('b1', 'idle')]
    expect(staysHome(home, {}, asksMore)).toBe(home)
    expect(frontOf(asksMore, '', held(home, asksMore))).toBe('b0')
    // Another approval of it waits behind that board and brings nobody forward.
    const twoApprovals = [bot('b0', 'needs', 'a0', 'a5'), bot('b1', 'idle')]
    expect(frontOf(twoApprovals, '', held(home, twoApprovals))).toBe('')
    // The question ends and the approval still waits: home again.
    expect(frontOf(alone, 'b0', held(staysHome(home, {}, alone), alone))).toBe('')
    // That ask ends: nothing holds the Bot for its next one.
    const next = [bot('b0', 'needs', 'a9'), bot('b1', 'idle')]
    expect(staysHome(home, {}, next)).toEqual({})
    expect(frontOf(next, '', held(staysHome(home, {}, next), next))).toBe('b0')
  })
  it('stands the one in front whole inside the view\'s margin: its body, its board and its feet', () => {
    for (const lookYaw of [0, 40]) for (const block of [null, 'tray', 'keys'] as const) {
      const s = stage(ask(['needs', 'working', 'working'], { block, lookYaw }))
      expect(s[0].forward).toBe(true)
      expect(inView(s[0].at, { yaw: lookYaw, pitch: 0 }, WITH_BOARD)).toBe(true)
    }
  })
  it('has the far one between step beside the one that came forward, and stay in view', () => {
    const a = ask(['needs', 'working', 'working', 'working', 'working', 'working', 'working'])
    const s = stage(a)
    expect(s[0].forward).toBe(true)
    // The place between the near ones is behind its board: it gives way, and nobody else moves.
    expect(s.map(x => x.moved)).toEqual([false, false, true, false, false, false, false])
    expect(onBox(s[2].at, FORWARD_BOX, 0)).toBe(false)
    expect(inView(s[2].at)).toBe(true)
    expect(noClash(s.slice(1).map(x => x.at))).toBe('')
  })
  it('brings the one that needs him to where he looks, not to the start view', () => {
    // His head is turned 40 degrees to the right when the ask comes.
    const look = { yaw: 40, pitch: 0 }
    const s = stage(ask(['needs', 'working', 'working'], { lookYaw: 40 }))
    expect(s[0].forward).toBe(true)
    expect(yawOf(s[0].at)).toBeCloseTo(40 + PLACES.forward.yawDeg, 1)
    expect(inView(s[0].at, look)).toBe(true)
    expect(inView(s[0].at)).toBe(false)
    // A Bot of the ring that stands where it comes gives way, and stays in what he sees then.
    const ring = stage(ask(['needs', ...Array(8).fill('working')], { lookYaw: 46 }))
    expect(ring[7].moved).toBe(true)
    expect(facesClash(ring[0].at, ring[7].at, 0)).toBe(false)
    // The page rule is judged where he looks: a page straight ahead does not stop one that comes to 40 degrees.
    const paged = stage(ask(['needs'], { lookYaw: 40, pages: [firstPage] }))
    expect(paged[0].forward).toBe(true)
    expect(paged[0].sign).toBeNull()
  })
  it('sends the one that needs him to the low side while a taken tray or the keyboard holds the middle', () => {
    for (const block of ['tray', 'keys'] as const) {
      const s = stage(ask(['needs', 'working', 'working'], { block }))
      expect(Math.abs(yawOf(s[0].at))).toBeGreaterThan(15)
      expect(inView(s[0].at)).toBe(true)
      // Nobody gives way: the middle is not its.
      expect(s[2].moved).toBe(false)
    }
  })
  it('stands the one that needs him on the side away from the card, never behind its keyboard', () => {
    // The keyboard as `useChatCard` places it: 25 degrees to the side of his look, 11 down, 0.8 m away.
    const keysAt = (yaw: number) => {
      const [r, d] = [(yaw * Math.PI) / 180, 0.8]
      return seenBox({ x: Math.sin(r) * d, y: PAGES.eyeM + d * Math.tan((-11 * Math.PI) / 180), z: -Math.cos(r) * d }, 0.34, 0.136)
    }
    // Looks of 0 and of 40 degrees; the card to his left and to his right; the asker's own place on either side.
    for (const lookYaw of [0, 40]) for (const cardSide of [-1, 1]) for (const states of [['needs', 'working', 'working'], ['working', 'needs', 'working']]) {
      const keys = keysAt(lookYaw + cardSide * 25)
      const s = stage(ask(states, { block: 'keys', keys, lookYaw }))
      const x = s[states.indexOf('needs')]
      expect(x.forward).toBe(true)
      expect(Math.sign(yawOf(x.at) - lookYaw)).toBe(-cardSide)
      expect(onBox(x.at, keys, 0, { left: 0.17, right: 0.17, top: 0.63, bottom: 0.04 })).toBe(false)
      expect(inView(x.at, { yaw: lookYaw, pitch: 0 })).toBe(true)
    }
    // With no keyboard told it takes the card's usual side: to his left, so the asker is to his right.
    expect(yawOf(stage(ask(['needs', 'working'], { block: 'keys' }))[0].at)).toBeGreaterThan(15)
  })
  it('stands the one that needs him on either side of a taken tray: the side where it covers no face', () => {
    // Its own place is to the left, where the place of the second Bot's kept point would be behind it: it takes the right.
    const left = spotOf(0)
    const behind: Pt = [left[0] * 1.3, left[1] + 0.05, left[2] * 1.3]
    const a = ask(['needs', 'working'], { block: 'tray', homes: [left, behind] })
    const s = stage(a)
    expect(yawOf(s[0].at)).toBeGreaterThan(15)
    expect(s[1].moved).toBe(false)
    expect(facesClash(s[0].at, s[1].at, 0)).toBe(false)
    // With nobody behind it, it stays on its own side.
    expect(yawOf(stage(ask(['needs'], { block: 'tray', homes: [left] }))[0].at)).toBeLessThan(-15)
  })
  it('stands everyone at the desk and brings nobody forward while the states cannot be known', () => {
    const a = ask(['needs', 'idle'], { known: false })
    const s = stage(a)
    expect(s.map(x => x.forward)).toEqual([false, false])
    expect(s.map(x => x.at)).toEqual(a.homes)
  })
})

describe('a page he has open, and Home\'s large surface', () => {
  it('is covered by no Bot, its corners too: one that it would cover stands beside it, and the desks stay where they are', () => {
    for (const states of [SEVEN, TWELVE]) {
      const a = ask(states, { pages: [firstPage] })
      const s = stage(a)
      // In the page's own frame, with no air: the body's whole rectangle is outside the page's.
      for (const x of s) expect(onBox(x.at, firstPage, 0)).toBe(false)
      expect(noClash(s.map(x => x.at))).toBe('')
      // One that gave way is whole in the level view or whole outside it, never cut by its edge.
      const edge = Math.tan((PLACES.viewHalfYaw * Math.PI) / 180)
      for (const x of s.filter(y => y.moved)) {
        const r = rectOf(x.at)
        expect(inView(x.at) || r === null || r.x0 > edge || r.x1 < -edge).toBe(true)
      }
      s.forEach((x, i) => expect(x.desk.at).toEqual(deskAt(a.homes[i])))
      // A desk that the page covers is not drawn while it shows; one outside it is.
      expect(s.some(x => x.desk.hidden)).toBe(true)
      const away = (x: (typeof s)[number]) => Math.hypot(x.at[0] - x.desk.at[0], x.at[1] + PLACES.desk.topM - x.desk.at[1], x.at[2] - x.desk.at[2])
      for (const x of s) expect(x.desk.hidden).toBe(onBox(x.desk.at, firstPage, 0, { left: 0.065, right: 0.065, top: 0.03, bottom: 0.09 }) || x.moved)
      // No desk stands empty: each one that is drawn has its Bot within an idle step of it.
      for (const x of s.filter(y => !y.desk.hidden)) expect(away(x)).toBeLessThanOrEqual(0.25)
    }
  })
  it('keeps the one that needs him off the page he reads: no place in view, so its sign stands at the page edge', () => {
    const s = stage(ask(['working', 'needs'], { pages: [firstPage] }))
    expect(s[1].forward).toBe(false)
    expect(onBox(s[1].at, firstPage, 0)).toBe(false)
    expect(s[1].sign).not.toBeNull()
  })
  it('counts every ask of the Bot on the sign at the page\'s edge, also the one its board shows', () => {
    expect(pageSignCount([{ id: 'a' }, { id: 'b' }, { id: 'c' }])).toBe(3)
    // Its one ask is on its board, in view: the sign still says 1, not 0.
    expect(pageSignCount([{ id: 'a' }])).toBe(1)
    expect(pageSignCount([])).toBe(1)
  })
  it('lets the one that needs him come beside a page that leaves room in its view', () => {
    const small = seenBox({ x: 0, y: PAGES.eyeM + 0.2, z: -PAGES.distanceM }, 0.5, 0.3)
    const s = stage(ask(['needs'], { pages: [small] }))
    expect(s[0].forward).toBe(true)
    expect(onBox(s[0].at, small, 0)).toBe(false)
  })
  it('clears the middle for Home\'s large surface when he opens it', () => {
    const s = stage(ask(Array(7).fill('working'), { homeLarge: true }))
    for (const x of s) expect(onBox(x.at, HOME_BOX, 0)).toBe(false)
    expect(noClash(s.map(x => x.at))).toBe('')
  })
})

describe('the life and the drag', () => {
  it('stands a Bot that he put down at the point where he let go, its desk beside it, until its state changes', () => {
    // The reviewer's case: an idle Bot of twelve, let go right of the middle.
    const drop: Pt = keptPoint([0.87, 1.26, -2.77])
    for (const state of ['idle', 'working', 'failed', 'needs']) {
      const states = Array.from({ length: 12 }, (_, i) => (i === 1 ? state : i % 3 === 0 ? 'working' : 'idle'))
      const a = ask(states, { homes: placeHomes(slugs(12), { b1: drop }), put: new Set(['b1']) })
      const s = stage(a)
      // Its body, not only its kept place, is at that point; and so is its desk.
      expect(s[1].at).toEqual(drop)
      expect(s[1].desk.at).toEqual(deskAt(drop))
      expect(s[1].forward).toBe(false)
      expect(s[1].moved).toBe(false)
      expect(s[1].nap).toBe(false)
      // Nobody else moves for it.
      s.forEach((x, i) => { if (i !== 1) expect(x.moved).toBe(false) })
    }
    // A page does not move it either: he put it there.
    const paged = stage(ask(['idle'], { homes: [[0, 1.3, -2]], pages: [firstPage], put: new Set(['b0']) }))
    expect(paged[0].at).toEqual([0, 1.3, -2])
    // Not put down, the same Bot gives way to the page.
    expect(stage(ask(['idle'], { homes: [[0, 1.3, -2]], pages: [firstPage] }))[0].moved).toBe(true)
  })
  it('gives each working Bot a beat of its own', () => {
    const at = (slug: string) => Array.from({ length: 22 }, (_, k) => leansNow(slug, k * 100))
    expect(at('atlas')).not.toEqual(at('otto'))
    expect(at('atlas').filter(Boolean).length).toBeGreaterThan(5)
  })
  it('keeps a dragged Bot at its distance from him, held to the near and far limits', () => {
    const home = spotOf(2)
    const r = reachOf(home)
    const mid = onSphere([0, PAGES.eyeM, 0], [0.3, -0.1, -1], r)
    expect(mid).not.toBeNull()
    const p = mid as Pt
    expect(Math.hypot(p[0], p[1] - PAGES.eyeM, p[2])).toBeCloseTo(r, 5)
    expect(reachOf(keptPoint([0, 1.4, -0.3]))).toBeCloseTo(PLACES.nearM, 1)
    expect(reachOf(keptPoint([0, 1.4, -9]))).toBeCloseTo(PLACES.farM, 1)
  })
})

describe('Home\'s box in the room of places (REVIEW-15 S2)', () => {
  it('is as tall as large Home with the most lines and the most filed lines', () => {
    const tallM = homeLayout('large', HOME.mostLines, true, false, { head: true, lines: HOME.doneLines }).heightM
    expect(HOME_BOX.y1 - HOME_BOX.y0).toBeCloseTo(tallM / HOME.distanceM, 9)
    // Taller than the thirteenth's box, which left out the filed lines.
    expect(tallM).toBeGreaterThan(homeLayout('large', HOME.mostLines, true, false).heightM)
  })
})
