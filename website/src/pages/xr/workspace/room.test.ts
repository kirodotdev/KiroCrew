import { describe, expect, it } from 'vitest'
import { CREW_BAND, CREW_ZONE } from './crewRow'
import { M_PER_PX, PAGES, RINGS, crossing, inZone } from './layout'
import { ASK_AGAIN, ROOM_VERSION, pagesToAskAgain, readCrew, readFront, readHome, readPlaces, readSeen, readSince, readPut, readRoom, readWalls, roomToKeep, settleRoom, startRoom, writeRoom, type KeptPage, type StandingPage } from './room'
import { MOST_LIVE_PAGES } from './source'

const chat: KeptPage = { path: '/chat/dashboard-ab12', at: { yawDeg: 0, pitchDeg: PAGES.lowerDeg, distanceM: 1.75 }, css: [1024, 720], byHand: false }
/** A page that a hand moved to the left, pulled nearer and made bigger. */
const settings: KeptPage = { path: '/settings/browser', at: { yawDeg: -47.25, pitchDeg: 6.5, distanceM: 1.312 }, css: [1400, 900], byHand: true }
const record = (pages: unknown, v: unknown = ROOM_VERSION) => JSON.stringify({ v, pages })

describe('the room that is kept', () => {
  it('is read back as it was kept', () => {
    expect(readRoom(writeRoom([chat, settings]))).toEqual([chat, settings])
    expect(readRoom(writeRoom([]))).toEqual([])
  })

  it('stands a page that no hand placed, kept level by an older build, at this build\'s default pitch (D74)', () => {
    const old = record([{ ...chat, at: { ...chat.at, pitchDeg: 0 } }, { ...settings, at: { ...settings.at, pitchDeg: 0 } }])
    const [mine, hers] = readRoom(old)
    expect(mine.at.pitchDeg).toBe(PAGES.lowerDeg)
    // A page a hand placed keeps its record, level or not.
    expect(hers.at.pitchDeg).toBe(0)
  })

  it('keeps a moved and resized page where it stood, at its size, as one that a hand placed', () => {
    const [back] = readRoom(writeRoom([settings]))
    expect(back.at).toEqual(settings.at)
    expect(back.css).toEqual([1400, 900])
    expect(back.byHand).toBe(true)
    const [opened] = startRoom([back], null)
    expect(opened.at).toEqual(settings.at)
    expect(opened.css).toEqual([1400, 900])
    expect(opened.byHand).toBe(true)
  })

  it('reads a kept page 390 px wide back at that width: an ask\'s panel that the person kept', () => {
    // It stood where it came up: nearer than the pages, straight ahead of where the person looked.
    const kept: KeptPage = { path: '/members?member=Mira', at: { yawDeg: 12, pitchDeg: -3, distanceM: 1.279 }, css: [390, 700], byHand: true }
    const [back] = readRoom(writeRoom([chat, kept])).slice(1)
    expect(back).toEqual(kept)
    expect(startRoom([chat, back], null)[1].css).toEqual([390, 700])
    // The least width of a page is 320 px: narrower than that is no size.
    expect(PAGES.minCssWidth).toBe(320)
    expect(readRoom(record([{ ...kept, css: [320, 700] }]))).toHaveLength(1)
    expect(readRoom(record([{ ...kept, css: [319, 700] }]))).toHaveLength(0)
  })

  it('keeps the path alone, and no more than twelve pages', () => {
    const text = writeRoom(Array.from({ length: 15 }, () => chat))
    expect(JSON.parse(text).pages).toHaveLength(MOST_LIVE_PAGES)
    expect(Object.keys(JSON.parse(text).pages[0]).sort()).toEqual(['at', 'byHand', 'css', 'path'])
    expect(readRoom(record(Array.from({ length: 15 }, () => chat)))).toHaveLength(MOST_LIVE_PAGES)
  })

  it('ignores a store that is not a room of this version', () => {
    for (const text of [null, undefined, '', 'not json', '[]', 'null', '7', '{}', record([chat], ROOM_VERSION + 1), record([chat], '1'), record('pages'), record({ 0: chat }), 'x'.repeat(70_000)]) expect(readRoom(text), String(text).slice(0, 30)).toEqual([])
  })

  it('drops a page that the gateway lets through in neither of its ways, or whose numbers are not a place and a size, with no word', () => {
    const bad: unknown[] = [
      { ...chat, path: '/deploy' }, { ...chat, path: '/XR/' }, { ...chat, path: '/settings/people' }, { ...chat, path: '/chat?token=abc' }, { ...chat, path: 'https://elsewhere.example/chat' }, { ...chat, path: 7 },
      { ...chat, at: { yawDeg: 400, pitchDeg: 0, distanceM: 1.75 } }, { ...chat, at: { yawDeg: 0, pitchDeg: 0, distanceM: 0.01 } }, { ...chat, at: { yawDeg: 0, pitchDeg: 0, distanceM: 90 } },
      { ...chat, at: { yawDeg: Number.NaN, pitchDeg: 0, distanceM: 1.75 } }, { ...chat, at: null }, { ...chat, at: { yawDeg: '0', pitchDeg: 0, distanceM: 1.75 } },
      { ...chat, css: [10, 10] }, { ...chat, css: [99999, 720] }, { ...chat, css: 'big' }, { ...chat, css: [1024] },
      null, 'page', 7, [],
    ]
    expect(readRoom(record([...bad, settings]))).toEqual([settings])
    // A page that only the gateway's guard refuses is kept: the gateway is asked, and says no when its guard is on.
    expect(readRoom(record([{ ...chat, path: '/capabilities' }]))).toEqual([{ ...chat, path: '/capabilities' }])
    // A page with more in it than a room keeps is read for what a room keeps, and `byHand` is true only when it says true.
    expect(readRoom(record([{ ...chat, byHand: 'yes', token: 'abc', query: '?x=1' }]))).toEqual([chat])
  })
})

describe('what the workspace opens with', () => {
  const W = PAGES.cssWidth * M_PER_PX, H = PAGES.cssHeight * M_PER_PX
  const front = { yawDeg: 0, pitchDeg: PAGES.lowerDeg, distanceM: PAGES.distanceM }

  it('opens no page when no room is kept and no page is asked for: no chat page comes by itself (T9)', () => {
    expect(startRoom([], null)).toEqual([])
    // A reload of a room with a record: the address adds no page, and a held one keeps the id `page`.
    expect(startRoom([], '/settings', true)).toEqual([])
    expect(startRoom([chat], '/chat', true).map(p => p.id)).toEqual(['page'])
  })

  it('is the page that the address asks for when no room is kept: the link of today', () => {
    expect(startRoom([], '/settings')).toEqual([{ id: 'page', path: '/settings', at: front, css: [PAGES.cssWidth, PAGES.cssHeight], byHand: false }])
  })

  it('is the kept room, as it was, when no page is asked for', () => {
    expect(startRoom([chat, settings], null).map(({ id: _id, ...page }) => page)).toEqual([chat, settings])
    expect(new Set(startRoom([chat, settings], null).map(p => p.id)).size).toBe(2)
  })

  it('adds the page that is asked for in a free place in front when the room does not hold it, and moves nothing', () => {
    const pages = startRoom([chat, settings], '/artifacts')
    expect(pages.slice(0, 2).map(({ id: _id, ...page }) => page)).toEqual([chat, settings])
    const added = pages[2]
    expect(added).toMatchObject({ id: 'page', path: '/artifacts', byHand: false })
    for (const other of pages.slice(0, 2)) expect(crossing({ at: added.at, widthM: W, heightM: H }, { at: other.at, widthM: other.css[0] * M_PER_PX, heightM: other.css[1] * M_PER_PX })).toBe(0)
    // Straight ahead is taken by the chat, and the left by Settings: the nearest free place is to the right.
    expect(added.at.yawDeg).toBe(60)
  })

  it('adds nothing when the room holds the page that is asked for, or a page under it', () => {
    expect(startRoom([chat, settings], '/chat')).toHaveLength(2)
    // The kept page that the address asks for is the one with the id `page`, as a new one would be.
    expect(startRoom([chat, settings], '/chat').map(p => p.id)).toEqual(['page', 'kept-2'])
    expect(startRoom([chat, settings], '/settings').map(p => p.id)).toEqual(['kept-1', 'page'])
    expect(startRoom([chat, settings], null).map(p => p.id)).toEqual(['kept-1', 'kept-2'])
    expect(startRoom([chat, settings], '/chat/dashboard-ab12')).toHaveLength(2)
    expect(startRoom([chat, settings], '/settings')).toHaveLength(2)
    expect(startRoom([chat, settings], '/settings/browser')).toHaveLength(2)
    // Another session, and another tab of Settings, are other pages.
    expect(startRoom([chat, settings], '/chat/another')).toHaveLength(3)
    expect(startRoom([chat, settings], '/settings/display')).toHaveLength(3)
    // `/chat` is not held by a page that only begins with those letters.
    expect(startRoom([{ ...chat, path: '/crew-board' }], '/chat')).toHaveLength(2)
  })
})

describe('what is kept of the open pages', () => {
  const eight: KeptPage[] = Array.from({ length: 8 }, (_, i) => ({ path: i ? `/chat/s-${i}` : '/settings/browser', at: { yawDeg: -70 + i * 20, pitchDeg: i % 2 ? 12.5 : -8, distanceM: 1.75 }, css: [1024 + i * 10, 720], byHand: i % 3 === 0 }))

  it('keeps a page of the record that the gateway had no room for, as it was, and not one that was opened in this visit', () => {
    const text = writeRoom(eight)
    const opened = startRoom(readRoom(text), null)
    expect(opened).toHaveLength(8)
    const fromRecord = new Set(opened.map(p => p.id))
    // A socket that dropped is still held by the gateway: four of the eight do not fit beside its pages.
    const open: StandingPage[] = opened.map(({ id, ...page }, i) => ({ id, page, full: i >= 4 }))
    expect(writeRoom(roomToKeep(open, fromRecord))).toBe(text)
    // Two pages opened by hand in this visit: the one with no room is not part of the room, the other is.
    const artifacts: KeptPage = { path: '/artifacts', at: { yawDeg: 100, pitchDeg: 0, distanceM: 1.75 }, css: [1024, 720], byHand: false }
    const schedule: KeptPage = { ...artifacts, path: '/schedule', at: { ...artifacts.at, yawDeg: 130 } }
    const kept = roomToKeep([...open, { id: 'open-1', page: artifacts, full: true }, { id: 'open-2', page: schedule, full: false }], fromRecord)
    expect(kept).toEqual([...eight, schedule])
  })

  it('asks again for a page of the record that had no room, eight times at most, and never for one opened in this visit', () => {
    const opened = startRoom(eight, null)
    const fromRecord = new Set(opened.map(p => p.id))
    const open: StandingPage[] = [
      ...opened.map(({ id, ...page }, i) => ({ id, page, full: i >= 6 })),
      { id: 'open-1', page: { ...eight[0], path: '/artifacts' }, full: true },
    ]
    const asked = new Map<string, number>()
    const waiting = opened.slice(6).map(p => p.id)
    for (let round = 0; round < ASK_AGAIN.most; round += 1) expect(pagesToAskAgain(open, fromRecord, asked)).toEqual(waiting)
    expect(pagesToAskAgain(open, fromRecord, asked)).toEqual([])
    expect(ASK_AGAIN).toEqual({ everyMs: 5000, most: 8 })
    // A page that got its room is asked for no more, whatever is left of its count.
    expect(pagesToAskAgain(open.map(p => ({ ...p, full: false })), fromRecord, new Map())).toEqual([])
  })

  it('keeps no page that was asked for by the address in this visit and had no room', () => {
    const opened = startRoom(eight, '/notifications')
    expect(opened).toHaveLength(9)
    // The first eight came from the record; the ninth is the page that the address asked for.
    const fromRecord = new Set(opened.slice(0, eight.length).map(p => p.id))
    const kept = roomToKeep(opened.map(({ id, ...page }) => ({ id, page, full: true })), fromRecord)
    expect(kept.map(p => p.path)).toEqual(eight.map(p => p.path))
  })
})

describe('the drawn room, as the record keeps it', () => {
  const chat: KeptPage = { path: '/chat', at: { yawDeg: 0, pitchDeg: PAGES.lowerDeg, distanceM: PAGES.distanceM }, css: [PAGES.cssWidth, PAGES.cssHeight], byHand: false }

  it('starts off, and is on only when the record says so', () => {
    expect(readWalls(null)).toBe(false)
    expect(readWalls('')).toBe(false)
    expect(readWalls('no record at all')).toBe(false)
    expect(readWalls(writeRoom([chat]))).toBe(false)
    expect(readWalls(writeRoom([chat], true))).toBe(true)
    expect(readWalls(writeRoom([], true))).toBe(true)
    // Anyone's text: a word that is not `true`, or a record of another version, is off.
    expect(readWalls(JSON.stringify({ v: ROOM_VERSION, pages: [], walls: 'yes' }))).toBe(false)
    expect(readWalls(JSON.stringify({ v: ROOM_VERSION + 1, pages: [], walls: true }))).toBe(false)
  })

  it('changes nothing of the pages that are kept, and a record with it off is the record of a build that has no room', () => {
    expect(readRoom(writeRoom([chat], true))).toEqual([chat])
    expect(writeRoom([chat], false)).toBe(writeRoom([chat]))
    expect(writeRoom([chat])).not.toContain('walls')
  })
})

describe('a kept room with a page in the crew\'s place', () => {
  const sized = (p: KeptPage) => ({ at: p.at, widthM: p.css[0] * M_PER_PX, heightM: p.css[1] * M_PER_PX })
  const clear = (pages: KeptPage[]) => pages.every((a, i) => pages.every((b, j) => j <= i || crossing(sized(a), sized(b)) === 0))

  it('leaves an ask\'s page that he made his own where it stood: it is kept as placed by his hand', () => {
    // A Bot's page came up in front of him, low, in the crew's place, and he kept it there (`useAskPanels.ts`, `keep`).
    const asked = { id: 'ask-1', page: { path: '/members?member=Juno', at: { yawDeg: 0, pitchDeg: -23, distanceM: 1.25 }, css: [PAGES.cssWidth, PAGES.cssHeight] as [number, number], byHand: true }, full: false }
    const record = writeRoom(roomToKeep([asked], new Set()))
    const back = settleRoom(readRoom(record))
    expect(back).toEqual([asked.page])
    // Kept as not placed by a hand, a load would stand it again, clear of the crew.
    expect(settleRoom([{ ...asked.page, byHand: false }])[0].at).not.toEqual(asked.page.at)
  })

  it('leaves a room alone that has no page there', () => {
    expect(settleRoom([chat, settings])).toEqual([chat, settings])
    expect(settleRoom([])).toEqual([])
  })

  it('stands the two rows of an older build again in the two rings, each page at its size', () => {
    // Seven pages as the older build stood them: six over level, and one in the low row, straight ahead.
    const old: KeptPage[] = [0, 60, -60, 120, -120, 180].map((yawDeg, i) => ({ path: `/chat/s-${i}`, at: { yawDeg, pitchDeg: 17, distanceM: 2.1 }, css: [1024 + i * 16, 720], byHand: false }))
    old.push({ path: '/settings', at: { yawDeg: 0, pitchDeg: -17, distanceM: 2.1 }, css: [1100, 760], byHand: false })
    const now = settleRoom(old)
    expect(now.map(p => p.path)).toEqual(old.map(p => p.path))
    expect(now.map(p => p.css)).toEqual(old.map(p => p.css))
    expect(now.filter(p => inZone(sized(p), CREW_ZONE))).toEqual([])
    expect(clear(now)).toBe(true)
    expect(new Set(now.map(p => p.at.pitchDeg))).toEqual(new Set([RINGS.level.pitchDeg, RINGS.upper.pitchDeg]))
    // It is done once: the room that comes out is left alone.
    expect(settleRoom(now)).toEqual(now)
  })

  it('moves a page that the layout stood behind the Bots to the free place nearest to its direction, and no page that a hand placed', () => {
    const low: KeptPage = { path: '/logs', at: { yawDeg: 50, pitchDeg: -17, distanceM: 2.1 }, css: [1024, 720], byHand: false }
    // The page that a hand pulled near reaches into the crew's place too: it stays where the hand put it.
    expect(inZone(sized(settings), CREW_ZONE)).toBe(true)
    const [first, moved, kept] = settleRoom([chat, low, settings])
    expect(first).toEqual(chat)
    expect(kept).toEqual(settings)
    expect(moved).toMatchObject({ path: '/logs', css: [1024, 720], byHand: false, at: { yawDeg: 60, pitchDeg: RINGS.level.pitchDeg } })
    for (const other of [first, kept]) expect(crossing(sized(moved), sized(other))).toBe(0)
  })

  it('leaves a page where it is when no place is clear for it', () => {
    // Six pages as big as a page can be, placed by hand all the way round: no row has a clear direction left.
    const wall: KeptPage[] = [0, 60, -60, 120, -120, 180].map(yawDeg => ({ path: '/overview', at: { yawDeg, pitchDeg: 0, distanceM: 1.75 }, css: [2560, 1600], byHand: true }))
    const low: KeptPage = { path: '/logs', at: { yawDeg: 0, pitchDeg: -17, distanceM: 2.1 }, css: [1024, 720], byHand: false }
    expect(settleRoom([...wall, low])).toEqual([...wall, low])
  })
})

describe('the places of the crew, as the record keeps them', () => {
  it('keeps a Bot\'s place in an optional field, and a record with none reads as before', () => {
    const text = writeRoom([], false, null, { atlas: 12.345, juno: -3 })
    expect(JSON.parse(text).crew).toEqual({ atlas: 12.35, juno: -3 })
    expect(readCrew(text)).toEqual({ atlas: 12.35, juno: -3 })
    expect(readRoom(text)).toEqual([])
    expect(JSON.parse(writeRoom([])).crew).toBeUndefined()
    expect(readCrew(writeRoom([]))).toEqual({})
  })
  it('drops a place out of the row\'s band, a value that is no number, and a record of another version', () => {
    const text = JSON.stringify({ v: ROOM_VERSION, pages: [], crew: { a: CREW_BAND[1] + 5, b: 'x', c: 1 } })
    expect(readCrew(text)).toEqual({ c: 1 })
    expect(readCrew(JSON.stringify({ v: ROOM_VERSION + 1, pages: [], crew: { c: 1 } }))).toEqual({})
    expect(readCrew('not json')).toEqual({})
  })
})

describe('the places with no row, as the record keeps them beside the row\'s yaws', () => {
  it('keeps both: a record written with places opened with the row reads its yaws, and the other way', () => {
    const text = writeRoom([], false, null, { atlas: 12.35 }, false, { atlas: [0.41234, 1.1, -2.2], juno: [-1, 0.9, -1.5] })
    // Opened with the row (`places` off): the yaws are as they were.
    expect(readCrew(text)).toEqual({ atlas: 12.35 })
    // Opened with no row: the points, rounded to the centimetre.
    expect(readPlaces(text)).toEqual({ atlas: [0.41, 1.1, -2.2], juno: [-1, 0.9, -1.5] })
    // Written again from what was read, with either switch, nothing of the other is lost.
    const again = writeRoom([], false, null, readCrew(text), false, readPlaces(text))
    expect(again).toBe(text)
    // A record of a build before places has no such field: no row opens with no kept point.
    expect(readPlaces(writeRoom([], false, null, { atlas: 3 }))).toEqual({})
  })
  it('keeps the mark of a Bot that stands where he put it, with the state it was in, over a reload', () => {
    const text = writeRoom([], false, null, {}, false, { atlas: [0.4, 1.1, -2.2], juno: [-1, 0.9, -1.5] }, { juno: 'idle', ghost: 'idle' })
    // A mark with no kept point is not written.
    expect(readPut(text)).toEqual({ juno: 'idle' })
    expect(readPlaces(text)).toEqual({ atlas: [0.4, 1.1, -2.2], juno: [-1, 0.9, -1.5] })
    // A state that is none, or a record with no marks, gives none.
    expect(readPut(JSON.stringify({ v: ROOM_VERSION, pages: [], places: { a: [0, 1, -2] }, put: { a: 'dancing' } }))).toEqual({})
    expect(readPut(writeRoom([], false, null, {}, false, { atlas: [0.4, 1.1, -2.2] }))).toEqual({})
    expect(readPut('not json')).toEqual({})
    // The asks whose board he handed back are kept by the Bot's slug, and what is no slug and ask is dropped.
    const sent = writeRoom([], false, null, {}, false, {}, {}, null, { juno: 'a:1' })
    expect(readHome(sent)).toEqual({ juno: 'a:1' })
    expect(JSON.parse(writeRoom([])).home).toBeUndefined()
    expect(readHome(JSON.stringify({ v: ROOM_VERSION, pages: [], home: { juno: 7, '': 'a', mira: '' } }))).toEqual({})
    expect(readHome('not json')).toEqual({})
    // The last visit and what is left to read of the one before (`since.ts`): kept whole, and none when there is none.
    const seen = { at: 1_795_000_000_000, bots: { juno: { asks: ['a:1'], cuts: [], ts: 1_794_999_000 } } }
    const since = { at: 1_794_000_000_000, bots: { mira: { group: 'finished' as const, ids: [] } } }
    const visit = writeRoom([], false, null, {}, false, {}, {}, null, {}, seen, since)
    expect(readSeen(visit)).toEqual(seen)
    expect(readSince(visit)).toEqual(since)
    expect(readSeen(writeRoom([]))).toBeNull()
    expect(readSince(writeRoom([]))).toBeNull()
    expect(JSON.parse(writeRoom([])).seen).toBeUndefined()
    expect(readSeen(JSON.stringify({ v: ROOM_VERSION + 1, pages: [], seen }))).toBeNull()
    expect(readSeen('not json')).toBeNull()
    // A visit too long to keep is left out: the room's own record stays readable.
    const long = { at: 1_795_000_000_000, bots: Object.fromEntries(Array.from({ length: 64 }, (_, i) => [`bot-${i}`, { asks: Array.from({ length: 16 }, (_x, k) => `a:${'x'.repeat(60)}:${k}`), cuts: [], ts: 1 }])) }
    const crowded = writeRoom([], false, null, {}, false, { atlas: [0.4, 1.1, -2.2] }, {}, null, {}, long, since)
    expect(crowded.length).toBeLessThan(64_000)
    expect(readSeen(crowded)).toBeNull()
    expect(readSince(crowded)).toBeNull()
    expect(readPlaces(crowded)).toEqual({ atlas: [0.4, 1.1, -2.2] })
    expect(readSince('not json')).toBeNull()
  })
  it('keeps the Bot in front with the ask it came for and the look it came to', () => {
    const text = writeRoom([], false, null, {}, false, {}, {}, { slug: 'mira', ask: 'need-7', yaw: 40.04 })
    expect(readFront(text)).toEqual({ slug: 'mira', ask: 'need-7', yaw: 40 })
    // No one in front, an old record, a broken field: none.
    expect(readFront(writeRoom([]))).toBeNull()
    expect(readFront(JSON.stringify({ v: ROOM_VERSION, pages: [], front: { slug: 'mira', ask: '', yaw: 40 } }))).toBeNull()
    expect(readFront(JSON.stringify({ v: ROOM_VERSION, pages: [], front: { slug: 'mira', ask: 'a', yaw: 'x' } }))).toBeNull()
    expect(readFront(JSON.stringify({ v: ROOM_VERSION + 1, pages: [], front: { slug: 'mira', ask: 'a', yaw: 4 } }))).toBeNull()
    expect(readFront('not json')).toBeNull()
  })
  it('drops a point that is no three numbers or out of the room', () => {
    const text = JSON.stringify({ v: ROOM_VERSION, pages: [], places: { a: [0, 1, -2], b: [0, 1], c: [9, 1, -2], d: 'x', e: [0, null, -1] } })
    expect(readPlaces(text)).toEqual({ a: [0, 1, -2] })
    expect(readPlaces(JSON.stringify({ v: ROOM_VERSION + 1, pages: [], places: { a: [0, 1, -2] } }))).toEqual({})
    expect(readPlaces('not json')).toEqual({})
  })
})

describe('his notes on boards in the room\'s record (M45)', () => {
  it('keeps a note and the put-by mark by ask id, and reads them back', async () => {
    const { readNotes, writeRoom } = await import('./room')
    const text = writeRoom([], false, null, {}, false, {}, {}, null, {}, null, null, { 'a:1': { note: 'Ship it after lunch', put: true }, 'a:2': { note: 'half', put: false } })
    expect(readNotes(text)).toEqual({ 'a:1': { note: 'Ship it after lunch', put: true }, 'a:2': { note: 'half', put: false } })
  })
  it('writes the newest 8 notes, not the oldest', async () => {
    const { readNotes, writeRoom } = await import('./room')
    const ten = Object.fromEntries(Array.from({ length: 10 }, (_, i) => [`a:${i}`, { note: `n${i}`, put: false, bot: 'atlas' }]))
    expect(Object.keys(readNotes(writeRoom([], false, null, {}, false, {}, {}, null, {}, null, null, ten)))).toEqual(['a:2', 'a:3', 'a:4', 'a:5', 'a:6', 'a:7', 'a:8', 'a:9'])
  })
  it('reads none from a record without them, and drops an entry that is no note', async () => {
    const { readNotes, writeRoom } = await import('./room')
    expect(readNotes(writeRoom([]))).toEqual({})
    expect(readNotes(null)).toEqual({})
    const text = writeRoom([], false, null, {}, false, {}, {}, null, {}, null, null, { 'a:1': { note: '  ', put: false } })
    expect(readNotes(text)).toEqual({})
  })
})

