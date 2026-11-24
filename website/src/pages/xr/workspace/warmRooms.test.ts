import { describe, expect, it } from 'vitest'
import { MOST_LIVE_PAGES } from './source'
import { AHEAD, ROOM_PAGES_MOST, filled, fitWarm, giveWay, holding, mayWarm, nextAhead, takeWarm, type WarmRoom } from './warmRooms'

const room = (id: string, leftAt: number, ...paths: string[]): WarmRoom => ({ room: id, leftAt, pages: paths.map((path, i) => ({ id: `${id}-${i}`, path, css: [1000, 700] })) })
const ids = (rooms: readonly WarmRoom[]) => rooms.map(r => r.room)

describe('warm rooms: the rules', () => {
  it('holds ten of the gateway\'s twelve for rooms, so two stay free for a page he opens and a Bot\'s page', () => {
    expect(ROOM_PAGES_MOST).toBe(MOST_LIVE_PAGES - 2)
    expect(ROOM_PAGES_MOST).toBe(10)
  })

  it('keeps a room\'s pages only when every one is live and was whole', () => {
    expect(mayWarm([{ live: true, whole: true }, { live: true, whole: true }])).toBe(true)
    // One page had not filled yet: it would come back as its shell, so the room is not kept at all.
    expect(mayWarm([{ live: true, whole: true }, { live: true, whole: false }])).toBe(false)
    expect(mayWarm([{ live: false, whole: true }])).toBe(false)
    expect(mayWarm([])).toBe(false)
  })

  it('keeps the rooms he left most recently, each whole or not at all, inside ten with the standing room', () => {
    const a = room('a', 100, '/chat', '/schedule', '/projects')
    const b = room('b', 200, '/members', '/sessions', '/crons', '/artifacts')
    const c = room('c', 300, '/settings', '/notifications')
    // He stands in a room of three: c (2) and b (4) fit beside it, a (3) would make twelve.
    expect(ids(fitWarm([a, b, c], 3).keep)).toEqual(['c', 'b'])
    expect(ids(fitWarm([a, b, c], 3).close)).toEqual(['a'])
    // Three rooms of three pages are nine: all stay.
    expect(ids(fitWarm([room('x', 1, '/a', '/b', '/c'), room('y', 2, '/d', '/e', '/f')], 3).keep)).toEqual(['y', 'x'])
    // Two rooms of six are twelve: the room he left is cold, whole.
    expect(fitWarm([room('six', 1, '/a', '/b', '/c', '/d', '/e', '/f')], 6)).toEqual({ keep: [], close: [expect.objectContaining({ room: 'six' })] })
    // A room that does not fit closes, and every room left before it: no older room stays for a newer one that went.
    const big = room('big', 250, '/1', '/2', '/3', '/4', '/5', '/6', '/7')
    expect(ids(fitWarm([a, big, c], 3).keep)).toEqual(['c'])
    expect(ids(fitWarm([a, big, c], 3).close)).toEqual(['big', 'a'])
  })

  it('gives way: the room left longest ago closes first, and only as many as the new page needs', () => {
    const a = room('a', 100, '/chat', '/schedule')
    const b = room('b', 200, '/members', '/sessions', '/crons')
    // Seven stand and five are warm: twelve. One more page needs a place: the oldest room closes, the other stays.
    expect(ids(giveWay([a, b], 12, 1))).toEqual(['a'])
    // Three more need a place: both close.
    expect(ids(giveWay([a, b], 12, 3))).toEqual(['a', 'b'])
    // There is room already: nothing closes.
    expect(giveWay([a, b], 10, 2)).toEqual([])
    // No warm room can make room for a standing room that is full by itself.
    expect(giveWay([], 12, 1)).toEqual([])
  })

  it('lets the record decide what comes back: a kept page takes a warm page of its path, each once', () => {
    const a = room('a', 100, '/chat', '/schedule', '/chat')
    const got = takeWarm([a], 'a', [{ path: '/chat' }, { path: '/projects' }, { path: '/chat' }])
    expect(got.taken.map(p => p?.id ?? null)).toEqual(['a-0', null, 'a-2'])
    // The schedule page is warm and not in the record any more (he closed it since): it closes.
    expect(got.left.map(p => p.id)).toEqual(['a-1'])
    expect(got.robbed).toEqual([])
    // A room with no warm page: every page comes fresh.
    expect(takeWarm([a], 'z', [{ path: '/projects' }])).toEqual({ taken: [null], left: [], robbed: [] })
  })

  it('takes a page from another warm room when its own has none: one page is never open twice, and that room is cold', () => {
    const a = room('a', 100, '/schedule')
    const b = room('b', 200, '/chat', '/members')
    const got = takeWarm([a, b], 'a', [{ path: '/chat' }, { path: '/schedule' }])
    expect(got.taken.map(p => p?.id ?? null)).toEqual(['b-0', 'a-0'])
    expect(got.robbed).toEqual(['b'])
    expect(got.left).toEqual([])
  })

  it('counts a page that is held out of sight as filled only by a still that came well after it was live', () => {
    // Not live, or no picture yet.
    expect(filled({ liveForMs: null, lastPictureMs: null, stillAfterLiveMs: null })).toBe(false)
    expect(filled({ liveForMs: 9000, lastPictureMs: null, stillAfterLiveMs: null })).toBe(false)
    // The shell's still: it came half a second after the page was live.
    expect(filled({ liveForMs: 5000, lastPictureMs: 4500, stillAfterLiveMs: 500 })).toBe(false)
    expect(filled({ liveForMs: 7000, lastPictureMs: 100, stillAfterLiveMs: AHEAD.afterLiveMs - 1 })).toBe(false)
    // A still that came 6 s after live: the page was at rest then. Moving pictures after it (a caret) do not undo it.
    expect(filled({ liveForMs: 9000, lastPictureMs: 200, stillAfterLiveMs: AHEAD.afterLiveMs })).toBe(true)
    // No late still at all (a page that never rests): it counts once it has been live long enough.
    expect(filled({ liveForMs: AHEAD.lateMs - 1, lastPictureMs: 100, stillAfterLiveMs: null })).toBe(false)
    expect(filled({ liveForMs: AHEAD.lateMs, lastPictureMs: 100, stillAfterLiveMs: null })).toBe(true)
  })

  it('warms ahead the first cold room whose pages fit inside ten, and none that does not', () => {
    const cold = [{ id: 'big', come: 7 }, { id: 'none', come: 0 }, { id: 'small', come: 2 }]
    // Four are held (standing and warm): seven more would be eleven, so the next that fits is taken.
    expect(nextAhead(cold, 4)?.id).toBe('small')
    expect(nextAhead(cold, 3)?.id).toBe('big')
    expect(nextAhead(cold, 9)).toBeNull()
    expect(nextAhead([], 0)).toBeNull()
  })

  it('finds the warm rooms that hold a page of a path', () => {
    const a = room('a', 100, '/chat', '/schedule')
    const b = room('b', 200, '/members')
    expect(ids(holding([a, b], '/schedule'))).toEqual(['a'])
    expect(holding([a, b], '/crons')).toEqual([])
  })
})
