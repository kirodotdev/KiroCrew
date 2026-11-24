import { describe, expect, it } from 'vitest'
import { readFront, readHome, readPlaces, readPut, readRoom, readSeen, readSince, readWalls, writeRoom, type KeptPage } from './room'
import { cleanName, copyName, signsOf, ROOMS_MOST, ROOM_NAME_MOST, addRoom, openRooms, roomOfRecord, standingFor, tagRoom, bringBack, cardThrown, keepStanding, layoutOf, planOf, proposeName, readRooms, removeRoom, renameRoom, startRooms, switchPlan, switchRoom, uniqueName, writeRooms, type Rooms } from './rooms'

const page = (path: string, yawDeg = 0, css: [number, number] = [1024, 720], byHand = false): KeptPage => ({ path, at: { yawDeg, pitchDeg: 0, distanceM: 1.75 }, css, byHand })
const seen = { at: 1_795_000_000_000, bots: { juno: { asks: ['a:1'], cuts: [], ts: 1_794_999_000 } } }
const since = { at: 1_794_000_000_000, bots: { mira: { group: 'finished' as const, ids: [] } } }
const front = { slug: 'juno', ask: 'a:1', yaw: 4 }
/** A standing record with everything in it: a layout, and the crew's state. */
const record = (pages: KeptPage[], places: Record<string, [number, number, number]> = {}) =>
  writeRoom(pages, true, null, { atlas: 9 }, false, places, Object.fromEntries(Object.keys(places).map(slug => [slug, 'idle'])), front, { mira: 'a:7' }, seen, since)
const titles: Record<string, string> = { '/chat': 'Chat', '/schedule': 'Schedule', '/artifacts': 'Artifacts' }
const name = (layout: string, n: number) => proposeName(layout, n, path => titles[path] ?? '', { room: k => `Room ${k}`, more: (title, count) => `${title} +${count}` })
const review = record([page('/chat', 0), page('/schedule', -40, [560, 700])], { juno: [0.5, 1.2, -2] })
const build = record([page('/chat', 20, [1280, 800], true), page('/artifacts', -30)])

describe('the rooms he keeps', () => {
  it('holds of a room its layout, and never the crew\'s state', () => {
    const layout = layoutOf(review)
    expect(readRoom(layout).map(p => p.path)).toEqual(['/chat', '/schedule'])
    expect(readPlaces(layout)).toEqual({ juno: [0.5, 1.2, -2] })
    expect(JSON.parse(layout).crew).toEqual({ atlas: 9 })
    // Not who stands in front, not what he handed back, not his last visit, not the marks, not the device's setup.
    expect(Object.keys(JSON.parse(layout)).sort()).toEqual(['crew', 'pages', 'places', 'v'])
    expect(readFront(layout)).toBeNull()
    expect(readHome(layout)).toEqual({})
    expect(readSeen(layout)).toBeNull()
    expect(readSince(layout)).toBeNull()
    expect(readPut(layout)).toEqual({})
    // A record that cannot be read is the empty room.
    expect(readRoom(layoutOf('not json'))).toEqual([])
    expect(readRoom(layoutOf(null))).toEqual([])
  })
  it('starts with one room: the one that stands, named from its pages', () => {
    const rooms = startRooms(null, review, name)
    expect(rooms.now).toBe('r1')
    expect(rooms.rooms.map(r => [r.id, r.name, r.named])).toEqual([['r1', 'Chat +1', false]])
    expect(startRooms(rooms, build, name)).toBe(rooms)
    expect(startRooms(null, null, name).rooms[0].name).toBe('Room 1')
  })
  it('proposes a name from the largest page, and keeps a name that he gave', () => {
    expect(name(layoutOf(record([page('/schedule', 0, [560, 700]), page('/artifacts', 30, [1280, 800])])), 2)).toBe('Artifacts +1')
    expect(name(layoutOf(record([page('/chat')])), 2)).toBe('Chat')
    expect(name(layoutOf(record([page('/unknown')])), 3)).toBe('Room 3')
    // A room that keeps itself follows its pages with its proposed name; a name of his stays.
    const rooms = startRooms(null, review, name)
    expect(keepStanding(rooms, build, name).rooms[0].name).toBe('Chat +1')
    expect(keepStanding(rooms, record([page('/artifacts')]), name).rooms[0].name).toBe('Artifacts')
    const his = renameRoom(rooms, 'r1', '  Review   room ')
    expect(his.rooms[0]).toMatchObject({ name: 'Review room', named: true })
    expect(keepStanding(his, record([page('/artifacts')]), name).rooms[0].name).toBe('Review room')
    expect(renameRoom(rooms, 'r1', '   ')).toBe(rooms)
    expect([...renameRoom(rooms, 'r1', 'x'.repeat(80)).rooms[0].name].length).toBe(ROOM_NAME_MOST)
  })
  it('adds a room with "+": the room as it stands, and he is in the new one', () => {
    const one = startRooms(null, review, name)
    const two = addRoom(one, review, name) as Rooms
    expect(two.now).toBe('r2')
    expect(two.rooms.map(r => r.id)).toEqual(['r1', 'r2'])
    expect(readRoom(two.rooms[1].layout).map(p => p.path)).toEqual(['/chat', '/schedule'])
    // Five at most.
    let full = two
    for (let i = 0; i < 3; i += 1) full = addRoom(full, review, name) as Rooms
    expect(full.rooms.length).toBe(ROOMS_MOST)
    expect(addRoom(full, review, name)).toBeNull()
  })
  it('never loses a room at a switch: the one he leaves is in the list before the other stands', () => {
    // He is in r2 and has changed it to `build`; r1 is `review`.
    const two = addRoom(startRooms(null, review, name), review, name) as Rooms
    const out = switchRoom(two, 'r1', build, name)
    if (!out) throw new Error('no switch')
    // The list, as written first: r2 holds what stood, and the list names r1 as the room that stands.
    expect(out.rooms.now).toBe('r1')
    expect(readRoom(out.rooms.rooms[1].layout).map(p => p.path)).toEqual(['/chat', '/artifacts'])
    expect(readRoom(out.rooms.rooms[1].layout)[0]).toMatchObject({ css: [1280, 800], byHand: true })
    // The record that stands then: r1's layout.
    expect(readRoom(out.stand).map(p => p.path)).toEqual(['/chat', '/schedule'])
    expect(readPlaces(out.stand)).toEqual({ juno: [0.5, 1.2, -2] })
    // And back: nothing of either room is lost.
    const back = switchRoom(out.rooms, 'r2', out.stand, name)
    expect(readRoom(back?.stand).map(p => p.path)).toEqual(['/chat', '/artifacts'])
    expect(readRoom(back?.rooms.rooms[0].layout ?? '').map(p => p.path)).toEqual(['/chat', '/schedule'])
    // No such room, and the room he is in: nothing happens.
    expect(switchRoom(two, 'r9', build, name)).toBeNull()
    expect(switchRoom(two, 'r2', build, name)).toBeNull()
  })
  it('carries the crew\'s state across a switch, in the record that stands, and copies none of it into a room', () => {
    const two = addRoom(startRooms(null, review, name), review, name) as Rooms
    const out = switchRoom(two, 'r1', build, name)
    if (!out) throw new Error('no switch')
    // His last visit, what changed since, who stands in front, what he handed back, the drawn room: as they were.
    expect(readSeen(out.stand)).toEqual(seen)
    expect(readSince(out.stand)).toEqual(since)
    expect(readFront(out.stand)).toEqual(front)
    expect(readHome(out.stand)).toEqual({ mira: 'a:7' })
    expect(readWalls(out.stand)).toBe(true)
    // The marks of Bots that stand where he put them do not go with a switch.
    expect(readPut(out.stand)).toEqual({})
    // No room of the list holds any of it.
    for (const room of out.rooms.rooms) expect(Object.keys(JSON.parse(room.layout)).every(k => ['v', 'pages', 'pagesHidden', 'crew', 'places'].includes(k))).toBe(true)
    expect(JSON.stringify(out.rooms)).not.toContain('"seen"')
    expect(JSON.stringify(out.rooms)).not.toContain('"front"')
    // An older build reads only the first key: it opens the room that stood last.
    expect(readRoom(out.stand).length).toBe(2)
  })
  it('removes a room and brings it back where it was; not the room he is in, not the last one', () => {
    let rooms = addRoom(addRoom(startRooms(null, review, name), review, name) as Rooms, build, name) as Rooms
    expect(rooms.rooms.map(r => r.id)).toEqual(['r1', 'r2', 'r3'])
    expect(removeRoom(rooms, 'r3')).toBeNull()
    const gone = removeRoom(rooms, 'r1')
    if (!gone) throw new Error('not removed')
    expect(gone.rooms.rooms.map(r => r.id)).toEqual(['r2', 'r3'])
    expect(bringBack(gone.rooms, gone.removed)?.rooms.map(r => r.id)).toEqual(['r1', 'r2', 'r3'])
    expect(bringBack(rooms, gone.removed)).toBeNull()
    // A new room after a removal takes a new id, so a card that comes back never meets its own id.
    expect((addRoom(gone.rooms, review, name) as Rooms).rooms.map(r => r.id)).toEqual(['r2', 'r3', 'r4'])
    rooms = startRooms(null, review, name)
    expect(removeRoom(rooms, 'r1')).toBeNull()
    expect(removeRoom(rooms, 'r7')).toBeNull()
  })
  it('reads a list that is whole, and drops what is none', () => {
    const rooms = renameRoom(addRoom(startRooms(null, review, name), build, name) as Rooms, 'r1', 'Review')
    const text = writeRooms(rooms)
    expect(readRooms(text)).toEqual(rooms)
    expect(readRooms(null)).toBeNull()
    expect(readRooms('not json')).toBeNull()
    expect(readRooms(JSON.stringify({ v: 2, now: 'r1', rooms: [] }))).toBeNull()
    expect(readRooms(JSON.stringify({ v: 1, now: 'r1', rooms: [] }))).toBeNull()
    // A room with a bad id or no name is dropped alone; a `now` that names no room is the first; a second of one id is dropped.
    const odd = readRooms(JSON.stringify({ v: 1, now: 'r9', rooms: [{ id: 'x', name: 'A', layout: '{}' }, { id: 'r2', name: 'B', layout: review }, { id: 'r2', name: 'C', layout: '{}' }, { id: 'r3', name: '', layout: '{}' }] }))
    expect(odd?.rooms.map(r => [r.id, r.name])).toEqual([['r2', 'B']])
    expect(odd?.now).toBe('r2')
    // What a list held of the crew's state by a hand's edit is dropped as it is read.
    expect(readSeen(odd?.rooms[0].layout)).toBeNull()
    // More than five are cut to five.
    const many = { v: 1, now: 'r1', rooms: Array.from({ length: 9 }, (_, i) => ({ id: `r${i + 1}`, name: `N${i}`, layout: '{}' })) }
    expect(readRooms(JSON.stringify(many))?.rooms.length).toBe(ROOMS_MOST)
  })
  it('plans a switch: the same path stays and moves, the others go and come', () => {
    const open = [{ id: 'a', path: '/chat' }, { id: 'b', path: '/schedule' }, { id: 'c', path: '/chat' }]
    const plan = switchPlan(open, [page('/chat', 20), page('/artifacts', -30)])
    expect(plan.stay.map(s => [s.id, s.to.at.yawDeg])).toEqual([['a', 20]])
    expect(plan.go).toEqual(['b', 'c'])
    expect(plan.come.map(p => p.path)).toEqual(['/artifacts'])
    // Two pages of one path in both rooms: each kept one takes one open one.
    const twice = switchPlan(open, [page('/chat', -10), page('/chat', 10)])
    expect(twice.stay.map(s => s.id)).toEqual(['a', 'c'])
    expect(twice.go).toEqual(['b'])
    expect(twice.come).toEqual([])
    // To the empty room: all go. From it: all come. Twelve to twelve others: twelve go, twelve come.
    expect(switchPlan(open, [])).toEqual({ stay: [], go: ['a', 'b', 'c'], come: [] })
    expect(switchPlan([], [page('/chat')]).come.length).toBe(1)
    const twelve = Array.from({ length: 12 }, (_, i) => ({ id: `p${i}`, path: `/a${i}` }))
    const other = switchPlan(twelve, Array.from({ length: 12 }, (_, i) => page(`/b${i}`, i * 10)))
    expect([other.stay.length, other.go.length, other.come.length]).toEqual([0, 12, 12])
    // The chat that went on to a session since is still the chat: it stays, after the pages of the same path.
    const moved = switchPlan([{ id: 'x', path: '/chat/a-session' }, { id: 'z', path: '/members?member=Juno' }], [page('/chat'), page('/schedule')])
    expect(moved.stay.map(s => [s.id, s.to.path])).toEqual([['x', '/chat']])
    expect(moved.go).toEqual(['z'])
    // But a kept page that names another session, Bot or tab is never stood in for: it comes fresh, the other goes.
    for (const [open1, kept1] of [['/chat/member-juno', '/chat/member-mira'], ['/members?member=Juno', '/members?member=Mira'], ['/settings/security', '/settings/models']]) {
      const other = switchPlan([{ id: 'o', path: open1 }], [page(kept1)])
      expect([other.stay.length, other.go, other.come.map(p => p.path)]).toEqual([0, ['o'], [kept1]])
    }
    // The exact page still wins over the page that moved on.
    const both = switchPlan([{ id: 'x', path: '/chat/a-session' }, { id: 'y', path: '/chat' }], [page('/chat')])
    expect([both.stay.map(s => s.id), both.go]).toEqual([['y'], ['x']])
  })
  it('draws a room\'s plan: each page where it stands round him', () => {
    const plan = planOf([page('/chat', 0), page('/schedule', -40, [560, 700]), page('/artifacts', 40, [560, 700])])
    // The page straight ahead is in the middle; the one to his left is left of it; all are inside the plan.
    expect(plan[0].x).toBeCloseTo(0.5, 6)
    expect(plan[1].x).toBeLessThan(plan[0].x)
    expect(plan[2].x).toBeGreaterThan(plan[0].x)
    expect(plan[0].w).toBeGreaterThan(plan[1].w)
    for (const r of plan) { expect(r.x).toBeGreaterThanOrEqual(0); expect(r.x).toBeLessThanOrEqual(1); expect(r.w).toBeGreaterThan(0); expect(r.h).toBeGreaterThan(0) }
    // A page behind him is drawn at the plan's edge, not outside it.
    expect(planOf([page('/chat', 170)])[0].x).toBe(1)
    expect(planOf([])).toEqual([])
  })
  it('reads a held card as thrown only when it went far AND was still fast at the release, as a board', () => {
    // In a session: the pinching hand's travel and its speed over its last 100 ms.
    expect(cardThrown({ handM: 0.12, handMps: 0.05 })).toBe(false) // a hand that drifts 12 cm slowly while he speaks
    expect(cardThrown({ handM: 0.4, handMps: 0.3 })).toBe(false) // far and slow: a carry
    expect(cardThrown({ handM: 0.03, handMps: 2 })).toBe(false) // fast and near: a twitch
    expect(cardThrown({ handM: 0.12, handMps: 1.2 })).toBe(true)
    // In the preview: how far the press went, its speed over its last movement, and how long before the up it last moved.
    expect(cardThrown({ px: 300, pxPerMs: 0.05, lastMs: 10 })).toBe(false)
    expect(cardThrown({ px: 300, pxPerMs: 2, lastMs: 400 })).toBe(false) // it stopped before he let go
    expect(cardThrown({ px: 20, pxPerMs: 2, lastMs: 5 })).toBe(false)
    expect(cardThrown({ px: 300, pxPerMs: 2, lastMs: 5 })).toBe(true)
    expect(cardThrown({})).toBe(false)
  })

  it('opens the room that the record says, when a tab died between the two writes of a switch', () => {
    const one = startRooms(null, tagRoom(review, 'r1'), name)
    const two = addRoom(one, tagRoom(review, 'r1'), name)!
    const went = switchRoom(two, 'r1', tagRoom(build, 'r2'), name)!
    expect(roomOfRecord(went.stand)).toBe('r1')
    // Killed after the list was written, before the record: the record still says r2, and r2 keeps its own pages.
    const opened = openRooms(went.rooms, tagRoom(build, 'r2'), name)
    expect(opened.now).toBe('r2')
    expect(readRoom(opened.rooms.find(k => k.id === 'r1')!.layout).map(p => p.path)).toEqual(['/chat', '/schedule'])
    expect(readRoom(opened.rooms.find(k => k.id === 'r2')!.layout).map(p => p.path)).toEqual(['/chat', '/artifacts'])
    // A record that an older build wrote has no tag: it is the room that the list says stands.
    expect(openRooms(went.rooms, build, name).now).toBe('r1')
    // The tag is not a layout: a room never carries another room's tag.
    expect(JSON.parse(went.rooms.rooms[1].layout).room).toBeUndefined()
    expect(readRoom(tagRoom(review, 'r1')).length).toBe(2)
  })

  it('never empties the room that stands when its record cannot be read, and tags a record that has no tag', () => {
    const two = addRoom(startRooms(null, tagRoom(review, 'r1'), name), tagRoom(review, 'r1'), name)!
    const kept = keepStanding({ ...two, now: 'r2' }, tagRoom(build, 'r2'), name)
    for (const bad of [null, '', '{"v":2,"pages":[]}', 'x'.repeat(70_000)]) {
      const opened = openRooms(kept, bad, name)
      expect(readRoom(opened.rooms.find(k => k.id === 'r2')!.layout).map(p => p.path)).toEqual(['/chat', '/artifacts'])
      // The record to stand is made from that layout, tagged with its room.
      const stand = standingFor(kept, bad)!
      expect(roomOfRecord(stand)).toBe('r2')
      expect(readRoom(stand).map(p => p.path)).toEqual(['/chat', '/artifacts'])
    }
    // A record that an older build wrote, with no tag: it is the room that the list says stands, and says so at once.
    expect(roomOfRecord(standingFor(kept, build)!)).toBe('r2')
    expect(readSeen(standingFor(kept, build)!)).toEqual(seen)
    // A tagged record that reads is left as it is; with no list there is nothing to mend.
    expect(standingFor(kept, tagRoom(build, 'r2'))).toBeNull()
    expect(standingFor(null, null)).toBeNull()
  })

  it('never writes the record of a room that another tab removed over the room that stands', () => {
    const page = (path: string) => ({ path, at: { yawDeg: 0, pitchDeg: 0, distanceM: 1.9 }, css: [1024, 720], byHand: true })
    const of = (...paths: string[]) => JSON.stringify({ v: 1, pages: paths.map(page) })
    // Tab A stood in r3 and wrote the record last; tab B, in r1, threw r3's card away.
    const kept: Rooms = { v: 1, now: 'r1', told: true, rooms: [{ id: 'r1', name: 'One', named: true, layout: of('/chat', '/crons') }, { id: 'r2', name: 'Two', named: true, layout: of('/settings') }] }
    const record = tagRoom(JSON.stringify({ v: 1, pages: [page('/sessions')], front: { bot: 'juno', ask: 'a1' } }), 'r3')
    const opened = openRooms(kept, record, (_l, n) => `Room ${n}`)
    expect(opened.now).toBe('r1')
    expect(readRoom(opened.rooms[0].layout).map(p => p.path)).toEqual(['/chat', '/crons'])
    expect(readRoom(opened.rooms[1].layout).map(p => p.path)).toEqual(['/settings'])
    // The record to stand: r1 from the list, with its tag, and the crew's state of the record that stood.
    const stand = standingFor(kept, record)
    expect(roomOfRecord(stand)).toBe('r1')
    expect(readRoom(stand).map(p => p.path)).toEqual(['/chat', '/crons'])
    expect(JSON.parse(stand ?? '{}').front).toEqual({ bot: 'juno', ask: 'a1' })
  })

  it('proposes for a new card a name that no other card has', () => {
    const page = { path: '/sessions', at: { yawDeg: 0, pitchDeg: 0, distanceM: 1.9 }, css: [1024, 720], byHand: true }
    const record = JSON.stringify({ v: 1, pages: [page] })
    const name = () => 'Sessions'
    const one = startRooms(null, record, name)
    const two = addRoom(one, record, name)
    expect(two?.rooms.map(k => k.name)).toEqual(['Sessions', 'Sessions (2)'])
    const three = addRoom(two as Rooms, record, name)
    expect(three?.rooms.map(k => k.name)).toEqual(['Sessions', 'Sessions (2)', 'Sessions (3)'])
    // The room that stands keeps its own number while its pages stay; a name he gave is taken as it is.
    expect(keepStanding(three as Rooms, record, name).rooms.map(k => k.name)).toEqual(['Sessions', 'Sessions (2)', 'Sessions (3)'])
    // Another room's name is refused: never two rooms with one name (D93).
    expect(renameRoom(three as Rooms, 'r3', 'Sessions').rooms[2]).toMatchObject({ name: 'Sessions (3)', named: false })
    expect(renameRoom(three as Rooms, 'r3', 'Mine').rooms[2]).toMatchObject({ name: 'Mine', named: true })
    // A long name is cut so that the number fits.
    expect(uniqueName('x'.repeat(ROOM_NAME_MOST), ['x'.repeat(ROOM_NAME_MOST)])).toBe(`${'x'.repeat(ROOM_NAME_MOST - 4)} (2)`)
  })
})

describe('a new room, a copy, and a name (D93)', () => {
  const page = { path: '/sessions', at: { yawDeg: 0, pitchDeg: 0, distanceM: 1.9 }, css: [1024, 720], byHand: true }
  const record = JSON.stringify({ v: 1, pages: [page] })
  const name = () => 'Sessions'
  const word = (n: number) => `Room ${n}`
  const one = startRooms(null, record, name)
  it('a new room has no page, a name of its own, and the old room is as it was and still stands', async () => {
    const { addEmptyRoom } = await import('./rooms')
    const got = addEmptyRoom(one, record, name, word)
    expect(got?.rooms.now).toBe('r1')
    expect(got?.rooms.rooms.map(k => [k.name, k.named, JSON.parse(k.layout).pages.length])).toEqual([['Sessions', false, 1], ['Room 2', true, 0]])
    // The name is its own: it does not follow pages later.
    const later = keepStanding({ ...(got as { rooms: Rooms }).rooms, now: 'r2' }, record, name)
    expect(later.rooms[1].name).toBe('Room 2')
  })
  it('never takes a name a room has: "Room 2" taken gives "Room 3"; three rooms are "Room 2", "Room 3"', async () => {
    const { addEmptyRoom } = await import('./rooms')
    const named = renameRoom(one, 'r1', 'Room 2')
    expect(addEmptyRoom(named, record, name, word)?.rooms.rooms.map(k => k.name)).toEqual(['Room 2', 'Room 3'])
    const two = addEmptyRoom(one, record, name, word) as { rooms: Rooms }
    expect(addEmptyRoom(two.rooms, record, name, word)?.rooms.rooms.map(k => k.name)).toEqual(['Sessions', 'Room 2', 'Room 3'])
  })
  it('a copy has the same pages and is named "<name> copy", with a number when that is taken', async () => {
    const { copyRoom } = await import('./rooms')
    const got = copyRoom(one, record, name, n => `${n} copy`) as { rooms: Rooms; id: string }
    expect(got.rooms.rooms.map(k => k.name)).toEqual(['Sessions', 'Sessions copy'])
    expect(got.rooms.rooms[1].layout).toBe(got.rooms.rooms[0].layout)
    expect(copyRoom(got.rooms, record, name, n => `${n} copy`)?.rooms.rooms[2].name).toBe('Sessions copy (2)')
  })
  it('keeps a name of 40 letters whole, cuts a longer one, and leaves the room\'s name at an empty one', async () => {
    const { nameTaken } = await import('./rooms')
    expect(renameRoom(one, 'r1', 'x'.repeat(40)).rooms[0].name).toHaveLength(40)
    expect(renameRoom(one, 'r1', 'x'.repeat(41)).rooms[0].name).toHaveLength(40)
    expect(renameRoom(one, 'r1', '   ').rooms[0]).toMatchObject({ name: 'Sessions', named: false })
    expect(nameTaken(one, 'r9', ' Sessions ')).toBe(true)
    expect(nameTaken(one, 'r1', 'Sessions')).toBe(false)
  })
})


describe('a room\'s name by whole signs (D98 6)', () => {
  const flag = '\u{1F1EF}\u{1F1F5}'
  const family = '\u{1F468}\u200D\u{1F469}\u200D\u{1F467}'
  it('counts a flag and a joined emoji as one sign each, and never keeps half of one', () => {
    expect(signsOf(`a${flag}${family}`)).toEqual(['a', flag, family])
    const name = cleanName(`${'x'.repeat(ROOM_NAME_MOST - 1)}${flag}${family}`)
    expect(signsOf(name)).toHaveLength(ROOM_NAME_MOST)
    expect(name.endsWith(flag)).toBe(true)
    expect(cleanName(flag.repeat(ROOM_NAME_MOST + 3))).toBe(flag.repeat(ROOM_NAME_MOST))
  })
  it('cuts the name, not the word, in the copy of a name of the most letters', () => {
    const word = (name: string) => `${name} copy`
    const long = 'n'.repeat(ROOM_NAME_MOST)
    const copy = copyName(long, word)
    expect(copy.endsWith(' copy')).toBe(true)
    expect(signsOf(copy)).toHaveLength(ROOM_NAME_MOST)
    expect(copyName('Chat', word)).toBe('Chat copy')
    // A second copy of it is told apart by a number, and still within the most.
    expect(signsOf(uniqueName(copy, [copy])).length).toBeLessThanOrEqual(ROOM_NAME_MOST)
    // A word that stands before the name, as some languages have it.
    expect(copyName(long, name => `Kopie von ${name}`).startsWith('Kopie von n')).toBe(true)
  })
  it('names a copy of a copy "name copy (2)", never "name copy copy", in each language\'s form of the word (D109 5)', async () => {
    const { copyRoom, copyParts } = await import('./rooms')
    const record = JSON.stringify({ v: 1, panels: [] })
    const room = (id: string, name: string) => ({ id, name, named: true, layout: record })
    const from = (now: string, ...names: string[]): Rooms => ({ v: 1, now, told: true, rooms: names.map((n, i) => room(`r${i + 1}`, n)) })
    const next = (rooms: Rooms, word: (n: string) => string) => copyRoom(rooms, record, (_l, n) => `Room ${n}`, word)?.rooms.rooms.at(-1)?.name
    for (const word of [(n: string) => `${n} copy`, (n: string) => `${n} Kopie`, (n: string) => `${n} のコピー`, (n: string) => `Kopie von ${n}`]) {
      const first = word('Chat')
      // He stands in the copy and copies it; then in that copy and copies again.
      expect(next(from('r2', 'Chat', first), word)).toBe(`${first} (2)`)
      expect(next(from('r3', 'Chat', first, `${first} (2)`), word)).toBe(`${first} (3)`)
      expect(copyParts(first, word)?.base).toBe('Chat')
      expect(copyParts(`${first} (2)`, word)?.base).toBe('Chat')
      expect(copyParts('Chat', word)).toBeNull()
      expect(copyParts('Chat (2)', word)).toBeNull()
    }
    const word = (n: string) => `${n} copy`
    // A name of the most signs: the word and the number stay whole, the name under them is cut.
    const long = 'Quarterly numbers and planning room 2026'
    const copy1 = next(from('r1', long), word) as string
    const copy2 = next(from('r2', long, copy1), word) as string
    const copy3 = next(from('r3', long, copy1, copy2), word) as string
    // The long name is cut once, so that every copy has the same name under the word.
    expect(copy1).toBe('Quarterly numbers and planning copy')
    expect(copy2).toBe('Quarterly numbers and planning copy (2)')
    expect(copy3).toBe('Quarterly numbers and planning copy (3)')
    expect(copy2).not.toContain('copy copy')
    expect(signsOf(copy2).length).toBeLessThanOrEqual(ROOM_NAME_MOST)
    expect(copyParts(copy2, word)?.tail).toBe(' copy (2)')
    // A name that he gave and that ends in the word reads as a copy; one where the word is part of a word does not.
    expect(next(from('r1', 'Hard copy'), word)).toBe('Hard copy (2)')
    expect(next(from('r1', 'Hardcopy'), word)).toBe('Hardcopy copy')
    expect(next(from('r1', 'copy'), word)).toBe('copy (2)')
    // A copy that he renamed is a room of its own name.
    expect(next(from('r2', 'Chat', 'Plans'), word)).toBe('Plans copy')
  })
  it('keeps the word and the number in the second copy of a name of the most letters (N7)', async () => {
    const { copyRoom } = await import('./rooms')
    const long = 'n'.repeat(ROOM_NAME_MOST)
    const record = JSON.stringify({ v: 1, panels: [] })
    const from: Rooms = { v: 1, now: 'r1', told: true, rooms: [{ id: 'r1', name: long, named: true, layout: record }, { id: 'r2', name: `${'n'.repeat(ROOM_NAME_MOST - 9)} copy`, named: true, layout: record }] }
    const got = copyRoom(from, record, (_l, n) => `Room ${n}`, n => `${n} copy`)
    const name = got?.rooms.rooms[2].name ?? ''
    expect(name.endsWith(' copy (2)')).toBe(true)
    expect(signsOf(name)).toHaveLength(ROOM_NAME_MOST)
  })
})
