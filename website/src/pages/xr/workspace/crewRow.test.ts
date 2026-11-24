import { describe, expect, it } from 'vitest'
import { M_PER_PX, PAGES, RINGS, inZone, placeNew, ring, type Standing } from './layout'
import { CREW, CREW_BAND, CREW_ZONE, STROLL, botPage, crewHomes, crewPlaces, crewPoint, crewRadiusM, crewSeen, crewStepDeg, freeYaw, isBotsPage, strollGoal, yawOnArc, type StrollAsk } from './crewRow'
import { LOOK } from './previewLook'
import { livePath } from './streamPath'
import { WALLS } from './walls'

const RAD = Math.PI / 180
const BODY_M = CREW.heightM * CREW.widthShare
/** How far under level the eye sees the bottom of the first page, and the end of the line a panel says under it. */
const PAGE_BOTTOM_DEG = Math.atan(((PAGES.cssHeight * M_PER_PX) / 2) / PAGES.distanceM) / RAD
const LINE_END_DEG = Math.atan(((PAGES.cssHeight * M_PER_PX) / 2 + CREW.lineUnderM) / PAGES.distanceM) / RAD

/** The least distance between the middles of two bodies, measured level. */
function leastApart(count: number): number {
  const at = crewPlaces(count).map(crewPoint)
  let least = Infinity
  for (let i = 0; i < at.length; i += 1) for (let j = i + 1; j < at.length; j += 1) least = Math.min(least, Math.hypot(at[i][0] - at[j][0], at[i][2] - at[j][2]))
  return least
}

describe('the places of the crew', () => {
  it('stands one Bot straight ahead, under the middle of the first page, 0.30 m nearer than it', () => {
    const [only] = crewPlaces(1)
    expect(crewPlaces(1)).toHaveLength(1)
    expect(only.yawDeg).toBe(0)
    expect(only.radiusM).toBeCloseTo(PAGES.distanceM - 0.3)
    expect(only.row).toBe(0)
    expect(crewRadiusM).toBeCloseTo(1.45)
  })

  it('stands twelve in one row on the arc, the sixth straight ahead, and no two bodies touch', () => {
    const places = crewPlaces(12)
    expect(places.every(p => p.row === 0 && p.radiusM === crewRadiusM)).toBe(true)
    expect(places[5].yawDeg).toBe(0)
    // An even row reaches half a place farther to the right than to the left: a Bot, and not a gap, is under the page's middle.
    expect(places[11].yawDeg).toBeCloseTo(-places[0].yawDeg + (places[1].yawDeg - places[0].yawDeg))
    // The order is the crew's own: from the left to the right.
    for (let i = 1; i < places.length; i += 1) expect(places[i].yawDeg).toBeGreaterThan(places[i - 1].yawDeg)
    // The whole row is no wider than 100 degrees.
    expect(places[11].yawDeg - places[0].yawDeg).toBeLessThan(100)
    expect(leastApart(12)).toBeGreaterThan(BODY_M)
    // The ring at a Bot's feet does not reach its neighbour's.
    expect(leastApart(12)).toBeGreaterThan(2 * CREW.ringM[1])
  })

  it('stands the thirteenth and the fourteenth in a second row behind, in the gaps, and no two bodies touch', () => {
    const places = crewPlaces(14)
    const front = places.filter(p => p.row === 0)
    const back = places.filter(p => p.row === 1)
    expect(front).toHaveLength(12)
    expect(back).toHaveLength(2)
    expect(back.every(p => p.radiusM > crewRadiusM + 0.2)).toBe(true)
    const step = front[1].yawDeg - front[0].yawDeg
    // Each one of the second row is halfway between two of the first.
    for (const b of back) expect(front.some(f => Math.abs(Math.abs(f.yawDeg - b.yawDeg) - step / 2) < 1e-9)).toBe(true)
    expect(leastApart(14)).toBeGreaterThan(BODY_M)
    // The eye sees the second row a little higher than the first: over the heads in front.
    expect(crewSeen(back[0]).topDeg).toBeLessThan(crewSeen(front[0]).topDeg)
  })

  it('stands no Bot in front of the first page, for 1, 12 and 14', () => {
    for (const count of [1, 12, 14]) {
      for (const p of crewPlaces(count)) {
        const seen = crewSeen(p)
        // The top of a head is under the page's bottom, as the eye sees it at the start.
        expect(seen.topDeg, `${count} Bots`).toBeGreaterThan(PAGE_BOTTOM_DEG)
        // In the front row it is under the line that a panel says under its page, and just under it.
        if (p.row === 0) { expect(seen.topDeg).toBeGreaterThan(LINE_END_DEG); expect(seen.topDeg).toBeLessThan(LINE_END_DEG + 1) }
      }
    }
  })

  it('hovers every Bot over the desk, and stands the row over the desk\'s top', () => {
    for (const p of crewPlaces(14)) {
      expect(p.feetM).toBeGreaterThan(WALLS.desk.topM)
      expect(p.feetM - WALLS.desk.topM).toBeLessThan(0.1)
      expect(p.radiusM).toBeGreaterThan(WALLS.desk.innerM)
      expect(p.radiusM).toBeLessThan(WALLS.desk.outerM)
    }
  })

  it('has the first page and the crew in the start picture: a lens 50 degrees tall, a level head, the frame shifted down', () => {
    expect(LOOK.startFovDeg).toBe(50)
    const t = Math.tan((LOOK.startFovDeg / 2) * RAD)
    // How far up and down the picture reaches, as the tangent of the angle from the level line.
    const up = t * (1 - LOOK.startShift)
    const down = t * (1 + LOOK.startShift)
    // The page's top is in it, with a little air.
    expect(up).toBeGreaterThan(Math.tan(PAGE_BOTTOM_DEG * RAD) + 0.004)
    // So are the feet of the five Bots in the middle of the row. A Bot off the middle is nearer to the lens's own
    // plane, and so stands lower in the picture.
    const seen = (p: { yawDeg: number; radiusM: number }) => p.radiusM * Math.cos(p.yawDeg * RAD)
    for (const p of crewPlaces(5)) expect((PAGES.eyeM - p.feetM) / seen(p)).toBeLessThan(down - 0.004)
    // Three bodies are whole across an upright phone's picture (390 by 760), and five across a desk's (1366 by 816).
    const side = (p: { yawDeg: number; radiusM: number }) => (p.radiusM * Math.sin(p.yawDeg * RAD) + BODY_M / 2) / seen(p)
    expect(side(crewPlaces(3)[2])).toBeLessThan(t * (390 / 760))
    expect(side(crewPlaces(5)[4])).toBeLessThan(t * (1366 / 816))
  })

  it('is no place for a number that is no count', () => {
    expect(crewPlaces(0)).toEqual([])
    expect(crewPlaces(-3)).toEqual([])
    expect(crewPlaces(2.9)).toHaveLength(2)
  })
})

describe('the page that a tap on a Bot opens', () => {
  it('is the Bots page with that Bot\'s name, and never the Bots page with no name', () => {
    expect(botPage({ name: 'Atlas' })).toBe('/members?member=Atlas')
    expect(botPage({ name: 'mate' })).toBe('/members?member=mate')
    for (const name of ['Atlas', 'mate', 'R&D / 100% Ops', 'a+b=c?', 'Zoë #1', 'it\'s (me)!*~']) {
      const page = botPage({ name })
      expect(page).not.toBeNull()
      expect(page).not.toBe('/members')
      // One pair and nothing else: every character that could begin another pair, or another address, is encoded.
      expect(page?.slice('/members?member='.length)).toMatch(/^[A-Za-z0-9_.~%-]+$/)
      expect(decodeURIComponent(page?.slice('/members?member='.length) ?? '')).toBe(name)
      // It is an address a panel may show, and it is kept and compared in this one spelling.
      expect(livePath(page)).toBe(page)
    }
  })

  it('spells a name as the gateway does, so that both sides compare one string', () => {
    // The gateway encodes everything but letters, digits and `_.-~`.
    expect(botPage({ name: 'R&D / 100% Ops' })).toBe('/members?member=R%26D%20%2F%20100%25%20Ops')
    expect(botPage({ name: 'it\'s (me)!*' })).toBe('/members?member=it%27s%20%28me%29%21%2A')
    // However an address arrives, the same spelling is kept.
    expect(livePath('/members?member=R%26D+%2F+100%25+Ops')).toBe('/members?member=R%26D%20%2F%20100%25%20Ops')
    expect(livePath('/members?member=%41tlas')).toBe('/members?member=Atlas')
  })

  it('opens nothing for a name that no address can carry', () => {
    for (const name of ['', ' lead', 'trail ', 'two\nlines', 'tab\there', 'x'.repeat(300)]) expect(botPage({ name })).toBeNull()
  })

  it('lets no other query through as a page a panel may show', () => {
    for (const path of ['/members?member=Atlas&create=1', '/members?create=1', '/members?member=Atlas#x', '/members?member=', '/members?member=A;b=1', '/members?member=%FF', '/members?Member=Atlas', '/chat?member=Atlas', '/members?member=Atlas?x']) expect(livePath(path), path).toBeNull()
    expect(livePath('/members')).toBe('/members')
  })
})

describe('the place that belongs to the crew', () => {
  const W = PAGES.cssWidth * M_PER_PX, H = PAGES.cssHeight * M_PER_PX

  it('holds every Bot of a front row of twelve, from the top of its head to the ring at its feet, and the desk', () => {
    for (const p of crewPlaces(CREW.frontMost)) {
      const half = Math.atan(CREW.ringM[1] / p.radiusM) / RAD
      expect(p.yawDeg - half).toBeGreaterThanOrEqual(CREW_ZONE.yawMinDeg - 1e-9)
      expect(p.yawDeg + half).toBeLessThanOrEqual(CREW_ZONE.yawMaxDeg + 1e-9)
      expect(-crewSeen(p).topDeg).toBeLessThanOrEqual(CREW_ZONE.pitchMaxDeg + 1e-9)
      expect(-crewSeen(p).feetDeg).toBeGreaterThan(CREW_ZONE.pitchMinDeg)
    }
    const desk = WALLS.desk
    expect(CREW_ZONE.yawMinDeg).toBeLessThanOrEqual(-desk.halfDeg)
    expect(CREW_ZONE.yawMaxDeg).toBeGreaterThanOrEqual(desk.halfDeg)
    // The desk's near edge, its lower rim, is the lowest that the eye sees of it.
    expect(CREW_ZONE.pitchMinDeg).toBeLessThanOrEqual(-Math.atan((PAGES.eyeM - desk.topM + desk.thickM) / desk.innerM) / RAD + 1e-9)
  })

  it('is under the first page and under every page of the level ring, and far under the ring over it', () => {
    const sized = (at: { yawDeg: number; pitchDeg: number; distanceM: number }) => ({ at, widthM: W, heightM: H })
    expect(inZone(sized({ yawDeg: 0, pitchDeg: PAGES.lowerDeg, distanceM: PAGES.distanceM }), CREW_ZONE)).toBe(false)
    for (const at of ring(12)) expect(inZone(sized(at), CREW_ZONE), JSON.stringify(at)).toBe(false)
    // The low row of an older build, straight ahead and at each side of it, is in it.
    for (const yawDeg of [0, 60, -60]) expect(inZone(sized({ yawDeg, pitchDeg: -17, distanceM: 2.1 }), CREW_ZONE)).toBe(true)
  })

  it('has no page in it with twelve pages open, one after the other, wherever the person looks', () => {
    for (const look of [0, 40, -75, 180]) {
      const pages: Standing[] = []
      for (let i = 0; i < 12; i += 1) {
        const got = placeNew(pages, W, H, look, [CREW_ZONE])
        expect(got.crosses).toBe(false)
        for (const m of got.moved) pages.find(x => x.id === m.id)!.at = m.at
        pages.push({ id: `n${i}`, at: got.at, widthM: W, heightM: H, user: false })
      }
      expect(pages.filter(p => inZone(p, CREW_ZONE))).toEqual([])
      expect(new Set(pages.map(p => p.at.pitchDeg))).toEqual(new Set([RINGS.level.pitchDeg, RINGS.upper.pitchDeg]))
    }
  })
})

describe('a Bot\'s page, for a second press on its seat', () => {
  const mira = { name: 'Mira', thread: 'member-mira', things: [{ slot: 'member-mira.task-1' }] }
  const juno = { name: 'Juno', thread: 'member-juno', things: [] }
  const crew = [mira, juno]
  it('is a panel that shows the Bot\'s page now, opened with it or not', () => {
    expect(isBotsPage(botPage(mira), false, mira, crew)).toBe(true)
    expect(isBotsPage(botPage(mira), true, mira, crew)).toBe(true)
  })
  it('is not a panel opened with it that moved to another Bot\'s page', () => {
    expect(isBotsPage(botPage(juno), true, mira, crew)).toBe(false)
    expect(isBotsPage(botPage(juno), true, juno, crew)).toBe(true)
  })
  it('is a panel that shows the chat of one of the Bot\'s sessions, and not of another Bot\'s', () => {
    expect(isBotsPage('/chat?sid=member-mira.task-1', false, mira, crew)).toBe(true)
    expect(isBotsPage('/chat?sid=member-mira', false, mira, crew)).toBe(true)
    expect(isBotsPage('/chat?sid=member-juno', true, mira, crew)).toBe(false)
    expect(isBotsPage('/chat?sid=member-juno', false, juno, crew)).toBe(true)
  })
  it('is a panel opened with it that went on to a page of no Bot, and no other panel', () => {
    expect(isBotsPage('/schedule', true, mira, crew)).toBe(true)
    expect(isBotsPage('/schedule', false, mira, crew)).toBe(false)
    expect(isBotsPage(null, false, mira, crew)).toBe(false)
  })
})

describe('a Bot put down in the row', () => {
  const step = crewStepDeg(crewRadiusM)
  it('keeps the place where it was let go when that is free, inside the band', () => {
    expect(freeYaw(10, [0, -step], step)).toBeCloseTo(10, 6)
    expect(freeYaw(CREW_BAND[1] + 30, [], step)).toBeCloseTo(CREW_BAND[1], 6)
  })
  it('lands at the nearest free place when it is let go nearer than one step to another Bot, and nobody else moves', () => {
    const taken = [0, step]
    expect(freeYaw(step * 0.4, taken, step)).toBeCloseTo(-step, 6)
    expect(freeYaw(step * 0.8, taken, step)).toBeCloseTo(2 * step, 6)
  })
  it('stands every Bot at its index place when nothing is kept, as before', () => {
    const slugs = ['a', 'b', 'c', 'd']
    expect(crewHomes(slugs).map(p => p.yawDeg)).toEqual(crewPlaces(4).map(p => p.yawDeg))
  })
  it('stands a Bot where a drag kept it, and a new Bot at the first free place; a slug that left is no place', () => {
    const base = crewPlaces(3).map(p => p.yawDeg)
    const homes = crewHomes(['a', 'b', 'c'], { a: base[2], gone: base[0] })
    expect(homes[0].yawDeg).toBeCloseTo(base[2], 6)
    expect(homes[1].yawDeg).toBeCloseTo(base[0], 6)
    expect(homes[2].yawDeg).toBeCloseTo(base[1], 6)
    const yaws = homes.map(p => p.yawDeg).sort((x, y) => x - y)
    for (let i = 1; i < yaws.length; i += 1) expect(yaws[i] - yaws[i - 1]).toBeGreaterThanOrEqual(step - 1e-6)
  })
  it('goes back with twelve Bots: dropped between two, no place is free, and no two yaws are under one step apart', () => {
    const twelve = crewPlaces(12).map(p => p.yawDeg)
    const others = twelve.filter((_y, i) => i !== 3)
    // The fourth Bot let go between the sixth and the seventh: its own place is the only free one, and it is not that.
    const between = (twelve[5] + twelve[6]) / 2
    expect(freeYaw(between, others, step)).toBeCloseTo(twelve[3], 6)
    expect(freeYaw(between, twelve, step)).toBeNull()
    const slugs = twelve.map((_y, i) => `b${i}`)
    const homes = crewHomes(slugs, { b3: between })
    const yaws = homes.map(p => p.yawDeg).sort((x, y) => x - y)
    for (let i = 1; i < yaws.length; i += 1) expect(yaws[i] - yaws[i - 1]).toBeGreaterThanOrEqual(step - 1e-6)
  })
  it('moves nobody else when one Bot is put down past the row and the row keeps every place it has', () => {
    const slugs = ['a', 'b', 'c', 'd', 'e', 'f', 'g']
    const before = crewHomes(slugs).map(p => p.yawDeg)
    const kept = Object.fromEntries(slugs.map((s, i) => [s, before[i]]))
    kept.a = CREW_BAND[0]
    const after = crewHomes(slugs, kept).map(p => p.yawDeg)
    expect(after[0]).toBeCloseTo(CREW_BAND[0], 6)
    for (let i = 1; i < slugs.length; i += 1) expect(after[i]).toBeCloseTo(before[i], 6)
  })
  it('reads where a ray from the head meets the row, as a yaw', () => {
    const at = crewPoint({ yawDeg: 20, radiusM: crewRadiusM, feetM: 0.5, row: 0 })
    const yaw = yawOnArc([0, 1.5, 0], [at[0], at[1] - 1.5, at[2]], crewRadiusM)
    expect(yaw).toBeCloseTo(20, 6)
    expect(yawOnArc([0, 1.5, 0], [0, -1, 0], crewRadiusM)).toBeNull()
  })
})

describe('what a Bot does of its own in the row', () => {
  const BODY = CREW.heightM * CREW.widthShare
  const placeAt = (yawDeg: number) => crewPoint({ yawDeg, radiusM: crewRadiusM, feetM: 0.5, row: 0 })
  const ask = (over: Partial<StrollAsk> = {}): StrollAsk => ({
    home: placeAt(0), head: [0, 1.5, 0], state: 'idle', rest: false,
    pick: { stroll: { key: 'look:1', angle: 0 }, nap: false }, ended: '', others: [], widthM: BODY, ...over,
  })
  /** How far along the row, to the right, a point is from a place: its offset on the arc's tangent there. */
  const alongOf = (from: readonly number[], to: readonly number[]) => {
    const yaw = Math.atan2(from[0], -from[2])
    return (to[0] - from[0]) * Math.cos(yaw) + (to[2] - from[2]) * Math.sin(yaw)
  }
  it('goes the whole way along a free row, and no farther', () => {
    const g = strollGoal(ask())
    expect(g.key).toBe('look:1')
    expect(alongOf(ask().home, g.at)).toBeCloseTo(STROLL.alongM, 6)
  })
  it('stays inside the band at both ends of the row', () => {
    for (const [yaw, angle] of [[CREW_BAND[1], 0], [CREW_BAND[0], Math.PI]] as const) {
      const g = strollGoal(ask({ home: placeAt(yaw), pick: { stroll: { key: 'look:2', angle }, nap: false } }))
      const at = Math.atan2(g.at[0], -g.at[2]) / (Math.PI / 180)
      expect(at).toBeGreaterThanOrEqual(CREW_BAND[0] - 1e-6)
      expect(at).toBeLessThanOrEqual(CREW_BAND[1] + 1e-6)
    }
  })
  it('takes at most half the air to its neighbour: two that stroll toward each other never touch, nor do their press boxes', () => {
    const step = crewStepDeg(crewRadiusM)
    // Two Bots two steps apart, each picking a stroll toward the other while the other stands at its place.
    const a = placeAt(0)
    const b = placeAt(2 * step)
    const ga = strollGoal(ask({ home: a, others: [b] }))
    const gb = strollGoal(ask({ home: b, others: [a], pick: { stroll: { key: 'look:1', angle: Math.PI }, nap: false } }))
    expect(Math.hypot(ga.at[0] - gb.at[0], ga.at[2] - gb.at[2])).toBeGreaterThanOrEqual(BODY)
    expect(alongOf(a, ga.at)).toBeGreaterThan(0)
    // The second picks later, against where the first now walks to.
    const gb2 = strollGoal(ask({ home: b, others: [ga.at], pick: { stroll: { key: 'look:1', angle: Math.PI }, nap: false } }))
    expect(Math.hypot(ga.at[0] - gb2.at[0], ga.at[2] - gb2.at[2])).toBeGreaterThanOrEqual(BODY)
  })
  it('takes half the air, not all of it, to a neighbour that stands at its stroll point with no goal', () => {
    const step = crewStepDeg(crewRadiusM)
    const a = placeAt(0)
    const b = placeAt(2 * step)
    // B was stopped on its way toward A: its body stands there, and it has no goal now.
    const bHalt = strollGoal(ask({ home: b, others: [a], pick: { stroll: { key: 'look:1', angle: Math.PI }, nap: false } })).at
    const ga = strollGoal(ask({ home: a, others: [bHalt] }))
    const air = alongOf(a, bHalt) - BODY
    expect(Math.hypot(ga.at[0] - bHalt[0], ga.at[2] - bHalt[2])).toBeGreaterThanOrEqual(BODY)
    expect(alongOf(a, ga.at)).toBeLessThanOrEqual(air / 2 + 1e-9)
    expect(alongOf(a, ga.at)).toBeGreaterThan(air / 4)
  })
  it('moves only nearer or farther in a full row', () => {
    const step = crewStepDeg(crewRadiusM)
    const g = strollGoal(ask({ others: [placeAt(step), placeAt(-step)], pick: { stroll: { key: 'look:3', angle: 0.3 }, nap: false } }))
    expect(Math.abs(alongOf(ask().home, g.at))).toBeLessThan(0.01)
    // With no side free, the look around shows in place: a turn of the body to the side it wanted, and no step.
    expect(g.at).toEqual([...ask().home])
    expect(g.turn).toBeCloseTo(STROLL.turnRad, 9)
    expect(strollGoal(ask({ others: [placeAt(step), placeAt(-step)], pick: { stroll: { key: 'look:4', angle: 2.8 }, nap: false } })).turn).toBeCloseTo(-STROLL.turnRad, 9)
  })
  it('strolls to the free side when the side it wanted is taken, and at least far enough to be seen', () => {
    const step = crewStepDeg(crewRadiusM)
    const g = strollGoal(ask({ others: [placeAt(step)], pick: { stroll: { key: 'look:5', angle: 0.2 }, nap: false } }))
    expect(g.turn ?? 0).toBe(0)
    expect(alongOf(ask().home, g.at)).toBeLessThanOrEqual(-STROLL.minAlongM + 1e-9)
    const near = strollGoal(ask({ pick: { stroll: { key: 'look:6', angle: Math.PI / 2 - 0.01 }, nap: false } }))
    expect(alongOf(ask().home, near.at)).toBeGreaterThanOrEqual(STROLL.minAlongM - 1e-9)
  })
  it('takes one step of 0.1 m toward his head while it asks', () => {
    const home = ask().home
    const g = strollGoal(ask({ state: 'needs' }))
    expect(g.key).toBe('needs')
    expect(Math.hypot(g.at[0] - home[0], g.at[2] - home[2])).toBeCloseTo(STROLL.askStepM, 6)
    expect(Math.hypot(g.at[0], g.at[2])).toBeLessThan(Math.hypot(home[0], home[2]))
  })
  it('rests at its place when its moves are off, while it works, and in a stroll that a pointer ended', () => {
    expect(strollGoal(ask({ rest: true })).key).toBe('home')
    expect(strollGoal(ask({ state: 'needs', rest: true })).key).toBe('home')
    expect(strollGoal(ask({ state: 'working' })).key).toBe('home')
    expect(strollGoal(ask({ ended: 'look:1' })).key).toBe('home')
    expect(strollGoal(ask({ ended: 'look:1', pick: { stroll: { key: 'look:2', angle: 0 }, nap: false } })).key).toBe('look:2')
    expect(strollGoal(ask({ pick: { stroll: null, nap: true } }))).toEqual({ key: 'home', at: [...ask().home], nap: true })
  })
})
