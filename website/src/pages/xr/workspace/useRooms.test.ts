import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ROOM_KEY } from './room'
import { ROOMS_KEY, readRooms, tagRoom } from './rooms'
import { botsStand, roomReady, useRooms, type RoomsHost } from './useRooms'
import type { PageSource } from './source'

const page = (path: string) => ({ path, at: { yawDeg: 0, pitchDeg: 0, distanceM: 1.9 }, css: [1024, 720], byHand: true })
const layout = (...paths: string[]) => JSON.stringify({ v: 1, pages: paths.map(page) })
const pathsOf = (id: string) => (JSON.parse(readRooms(localStorage.getItem(ROOMS_KEY))?.rooms.find(k => k.id === id)?.layout ?? '{}').pages ?? []).map((p: { path: string }) => p.path)

/** A workspace that stands in `r3` (`/sessions`); `placed` ends a switch's hold. */
function room() {
  let record = layout('/sessions')
  let placed: () => void = () => {}
  const said: string[] = []
  const mic: boolean[] = []
  const host: RoomsHost = {
    record: () => record,
    wrote: text => { record = text },
    open: () => [{ id: 'a', path: '/sessions' }],
    place: () => new Promise<void>(done => { placed = done }),
    setCrew: () => {}, setPlaces: () => {}, setAway: () => {},
    say: words => { said.push(words) }, micAway: away => { mic.push(away) },
  }
  return { host, end: () => placed(), said, mic }
}

describe('the rooms he keeps, in the workspace', () => {
  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem(ROOMS_KEY, JSON.stringify({ v: 1, now: 'r3', told: true, rooms: [{ id: 'r1', name: 'One', named: true, layout: layout('/chat', '/crons') }, { id: 'r3', name: 'Three', named: true, layout: layout('/sessions') }] }))
    localStorage.setItem(ROOM_KEY, tagRoom(layout('/sessions'), 'r3'))
  })

  it('adds no room while a switch holds: the room he goes to keeps its own pages', async () => {
    const { host, end } = room()
    const { result } = renderHook(() => useRooms(host))
    act(() => result.current.go('r1'))
    // The hold: the list says r1 stands, the pages are still r3's.
    act(() => result.current.add())
    expect(readRooms(localStorage.getItem(ROOMS_KEY))?.rooms.map(k => k.id)).toEqual(['r1', 'r3'])
    expect(pathsOf('r1')).toEqual(['/chat', '/crons'])
    expect(pathsOf('r3')).toEqual(['/sessions'])
    // Once the new room stands, "+" works again.
    await act(async () => { end(); await Promise.resolve() })
    act(() => result.current.add())
    expect(readRooms(localStorage.getItem(ROOMS_KEY))?.rooms.map(k => k.id)).toEqual(['r1', 'r3', 'r4'])
  })

  it('tells the crew where the new room keeps its Bots while the old room still stands', async () => {
    const calls: string[] = []
    let placed: () => void = () => {}
    const host: RoomsHost = {
      record: () => layout('/sessions'), wrote: () => {}, open: () => [{ id: 'a', path: '/sessions' }],
      place: (_go, _stay, _come, away, ahead) => { calls.push(`place away=${away}`); ahead(); calls.push('ahead done'); return new Promise<void>(done => { placed = () => { calls.push('old room gone'); done() } }) },
      setCrew: () => { calls.push('crew') }, setPlaces: () => { calls.push('places') }, setAway: on => { calls.push(`away ${on}`) },
    }
    const { result } = renderHook(() => useRooms(host))
    act(() => result.current.go('r1'))
    expect(calls).toEqual(['place away=false', 'crew', 'places', 'ahead done'])
    await act(async () => { placed(); await Promise.resolve() })
    expect(calls.slice(4)).toEqual(['old room gone', 'away false'])
  })

  it('holds the old room 1.2 s after each new page is live, so that the page has filled, and no longer than the bound', async () => {
    vi.useFakeTimers()
    try {
      // A new page as the switch holds it: not in the room, so nothing draws it and `pictureAt` stays null.
      const page = (s: { status: string }) => ({ status: () => s.status, pictureAt: () => null }) as unknown as PageSource
      const a = { status: 'live' }
      const b = { status: 'loading' }
      let ready = false
      void roomReady([page(a), page(b)]).then(() => { ready = true })
      await vi.advanceTimersByTimeAsync(600)
      b.status = 'live'
      // The later page is live: its body is still empty or half filled for about a second.
      await vi.advanceTimersByTimeAsync(1100)
      expect(ready).toBe(false)
      await vi.advanceTimersByTimeAsync(200)
      expect(ready).toBe(true)
      // A page that never opens holds the room 2.5 s at most.
      let never = false
      void roomReady([page({ status: 'loading' })]).then(() => { never = true })
      await vi.advanceTimersByTimeAsync(2400)
      expect(never).toBe(false)
      await vi.advanceTimersByTimeAsync(200)
      expect(never).toBe(true)
      // A page that is drawn (it has its first picture), a refused one and one with no such word do not hold it.
      let drawn = false
      void roomReady([{ status: () => 'live', pictureAt: () => 5 }, { status: () => 'failed', pictureAt: () => null }, { status: () => 'live' }] as unknown as PageSource[]).then(() => { drawn = true })
      await vi.advanceTimersByTimeAsync(0)
      expect(drawn).toBe(true)
      // No new page: nothing to wait for.
      let none = false
      void roomReady([]).then(() => { none = true })
      await vi.advanceTimersByTimeAsync(0)
      expect(none).toBe(true)
    } finally { vi.useRealTimers() }
  })

  it('holds the old room until every Bot has its whole size, and no longer than the bound', async () => {
    vi.useFakeTimers()
    try {
      let grows = [1, 1, 1]
      let stood = false
      void botsStand(() => grows).then(() => { stood = true })
      // The Bots have not begun to change spots yet: whole now is not "they stand".
      await vi.advanceTimersByTimeAsync(100)
      expect(stood).toBe(false)
      grows = [1, 0.4, 1]
      await vi.advanceTimersByTimeAsync(400)
      expect(stood).toBe(false)
      grows = [1, 1, 1]
      await vi.advanceTimersByTimeAsync(60)
      expect(stood).toBe(true)
      // A Bot that never stands does not hold the room for ever.
      let late = false
      void botsStand(() => [0.2], 500).then(() => { late = true })
      await vi.advanceTimersByTimeAsync(560)
      expect(late).toBe(true)
      // With no Bot there is nothing to wait for.
      let none = false
      void botsStand(() => []).then(() => { none = true })
      await vi.advanceTimersByTimeAsync(0)
      expect(none).toBe(true)
    } finally { vi.useRealTimers() }
  })

  it('offers no "+" while a removed room is offered back, so that it always has its place', () => {
    const { host } = room()
    const { result } = renderHook(() => useRooms(host))
    act(() => result.current.remove('r1'))
    expect(result.current.gone?.room.id).toBe('r1')
    act(() => result.current.add())
    expect(readRooms(localStorage.getItem(ROOMS_KEY))?.rooms.map(k => k.id)).toEqual(['r3'])
    act(() => result.current.back())
    expect(readRooms(localStorage.getItem(ROOMS_KEY))?.rooms.map(k => k.id)).toEqual(['r1', 'r3'])
  })

  it('keeps the card of the room he stands in when he throws it, and says why; another room\'s card goes', () => {
    const { host, said } = room()
    const { result } = renderHook(() => useRooms(host))
    act(() => result.current.hold('r3'))
    act(() => result.current.letGo('r3', true))
    expect(readRooms(localStorage.getItem(ROOMS_KEY))?.rooms.map(k => k.id)).toEqual(['r1', 'r3'])
    expect(said).toHaveLength(1)
    // The same line stands over the cards, which is where he sees it in a session.
    expect(result.current.said).toBe(said[0])
    expect(result.current.gone).toBeNull()
    act(() => result.current.hold('r1'))
    act(() => result.current.letGo('r1', true))
    expect(readRooms(localStorage.getItem(ROOMS_KEY))?.rooms.map(k => k.id)).toEqual(['r3'])
    expect(said).toHaveLength(1)
  })

  it('tells the new chat card\'s speech to stand aside while a card is in his hand', () => {
    const { host, mic } = room()
    const { result } = renderHook(() => useRooms(host))
    act(() => result.current.hold('r1'))
    expect(mic).toEqual([true])
    act(() => result.current.letGo('r1', false))
    expect(mic).toEqual([true, false])
  })

  it('stops a switch when the list could not be written: the room he leaves is not lost', () => {
    const { host } = room()
    const { result } = renderHook(() => useRooms(host))
    const before = localStorage.getItem(ROOM_KEY)
    const real = Storage.prototype.setItem
    Storage.prototype.setItem = () => { throw new DOMException('full', 'QuotaExceededError') }
    try { act(() => result.current.go('r1')) } finally { Storage.prototype.setItem = real }
    expect(result.current.rooms.now).toBe('r3')
    expect(localStorage.getItem(ROOM_KEY)).toBe(before)
    // The store takes writes again: the switch goes.
    act(() => result.current.go('r1'))
    expect(readRooms(localStorage.getItem(ROOMS_KEY))?.now).toBe('r1')
  })
})
describe('a limit is said (D98 5)', () => {
  it('says why at five rooms: "Copy this room" and "New room" make none', () => {
    const five = ['r1', 'r2', 'r3', 'r4', 'r5'].map((id, i) => ({ id, name: `Room ${i + 1}`, named: true, layout: layout('/chat') }))
    localStorage.clear()
    localStorage.setItem(ROOMS_KEY, JSON.stringify({ v: 1, now: 'r3', told: true, rooms: five }))
    localStorage.setItem(ROOM_KEY, tagRoom(layout('/chat'), 'r3'))
    const { host, said } = room()
    const { result } = renderHook(() => useRooms(host))
    act(() => result.current.copy())
    act(() => result.current.addEmpty())
    expect(readRooms(localStorage.getItem(ROOMS_KEY))?.rooms).toHaveLength(5)
    expect(said).toHaveLength(2)
    expect(said[0]).toContain('5')
    expect(result.current.said).toBe(said[1])
  })
  it('says why when the store is full: the name is not kept and the line stays with his letters', () => {
    localStorage.clear()
    localStorage.setItem(ROOMS_KEY, JSON.stringify({ v: 1, now: 'r3', told: true, rooms: [{ id: 'r1', name: 'One', named: true, layout: layout('/chat') }, { id: 'r3', name: 'Three', named: true, layout: layout('/sessions') }] }))
    localStorage.setItem(ROOM_KEY, tagRoom(layout('/sessions'), 'r3'))
    const { host, said } = room()
    const { result } = renderHook(() => useRooms(host))
    act(() => result.current.ask('r3'))
    act(() => result.current.typeName('r3', 'Ops'))
    const full = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('full', 'QuotaExceededError') })
    act(() => result.current.keepName('r3'))
    full.mockRestore()
    expect(said.at(-1)).toMatch(/storage is full/i)
    expect(result.current.nameAsk?.text).toBe('Ops')
    expect(readRooms(localStorage.getItem(ROOMS_KEY))?.rooms.find(k => k.id === 'r3')?.name).toBe('Three')
  })
  it.each([
    ['a switch', (r: ReturnType<typeof useRooms>) => r.go('r1')],
    ['a removal', (r: ReturnType<typeof useRooms>) => r.remove('r1')],
  ])('says that the store is full at %s that it stops, and the room stays (M67)', (_name, doIt) => {
    localStorage.clear()
    localStorage.setItem(ROOMS_KEY, JSON.stringify({ v: 1, now: 'r3', told: true, rooms: [{ id: 'r1', name: 'One', named: true, layout: layout('/chat') }, { id: 'r3', name: 'Three', named: true, layout: layout('/sessions') }] }))
    localStorage.setItem(ROOM_KEY, tagRoom(layout('/sessions'), 'r3'))
    const { host, said } = room()
    const { result } = renderHook(() => useRooms(host))
    const full = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('full', 'QuotaExceededError') })
    act(() => doIt(result.current))
    full.mockRestore()
    expect(said.at(-1)).toMatch(/storage is full/i)
    expect(result.current.rooms.now).toBe('r3')
    expect(readRooms(localStorage.getItem(ROOMS_KEY))?.rooms.map(k => k.id)).toEqual(['r1', 'r3'])
  })
  const two = () => {
    localStorage.clear()
    localStorage.setItem(ROOMS_KEY, JSON.stringify({ v: 1, now: 'r3', told: true, rooms: [{ id: 'r1', name: 'One', named: true, layout: layout('/chat') }, { id: 'r3', name: 'Three', named: true, layout: layout('/sessions') }] }))
    localStorage.setItem(ROOM_KEY, tagRoom(layout('/sessions'), 'r3'))
  }
  const fullFor = (only?: string) => { const real = Storage.prototype.setItem; return vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function setItem(this: Storage, key: string, value: string) { if (only && key !== only) { real.call(this, key, value); return } throw new DOMException('full', 'QuotaExceededError') }) }
  it('keeps the offer when "Back" meets a full store, and brings the room back once the store takes it (M86)', () => {
    two()
    const { host, said } = room()
    const { result } = renderHook(() => useRooms(host))
    act(() => result.current.remove('r1'))
    expect(result.current.gone?.room.name).toBe('One')
    const full = fullFor()
    act(() => result.current.back())
    full.mockRestore()
    expect(said.at(-1)).toMatch(/storage is full/i)
    expect(result.current.gone?.room.name).toBe('One')
    expect(result.current.rooms.rooms.map(k => k.id)).toEqual(['r3'])
    act(() => result.current.back())
    expect(result.current.gone).toBeNull()
    expect(result.current.rooms.rooms.map(k => k.id).sort()).toEqual(['r1', 'r3'])
  })
  it('says the line once when the layout\'s own save meets a full store, not at each save; again after a save went through (M86)', () => {
    two()
    const { host, said } = room()
    const { result } = renderHook(() => useRooms(host))
    const count = () => said.filter(w => /storage is full/i.test(w)).length
    let full = fullFor()
    act(() => result.current.keep(layout('/sessions', '/chat')))
    act(() => result.current.keep(layout('/sessions', '/crons')))
    act(() => result.current.keep(layout('/sessions', '/chat', '/crons')))
    full.mockRestore()
    expect(count()).toBe(1)
    act(() => result.current.keep(layout('/sessions', '/chat')))
    expect(pathsOf('r3')).toEqual(['/sessions', '/chat'])
    full = fullFor()
    act(() => result.current.keep(layout('/sessions')))
    full.mockRestore()
    expect(count()).toBe(2)
  })
  it.each([
    ['a switch', (r: ReturnType<typeof useRooms>) => r.go('r1')],
    ['"Add this room"', (r: ReturnType<typeof useRooms>) => r.add()],
  ])('checks the record\'s write as the list\'s at %s: it stops, says so, and the list is as it was (M86)', (_name, doIt) => {
    two()
    const { host, said } = room()
    const { result } = renderHook(() => useRooms(host))
    const before = localStorage.getItem(ROOMS_KEY)
    const full = fullFor(ROOM_KEY)
    act(() => doIt(result.current))
    full.mockRestore()
    expect(said.at(-1)).toMatch(/storage is full/i)
    expect(result.current.rooms.now).toBe('r3')
    expect(result.current.rooms.rooms.map(k => k.id)).toEqual(['r1', 'r3'])
    expect(readRooms(localStorage.getItem(ROOMS_KEY))).toEqual(readRooms(before))
    expect(localStorage.getItem(ROOM_KEY)).toBe(tagRoom(layout('/sessions'), 'r3'))
  })
})
