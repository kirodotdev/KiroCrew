import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PageSource, RestFacts } from './source'
import { freeRoomFor } from './fullHook'
import { IDLE_MS, TAB_GONE_MS, WARM_CHANNEL, useWarmRooms, type WarmEntry } from './useWarmRooms'

/** A page as the warm set sees it: live or not, whole or not; what it was told, and whether it was closed. */
function page(id: string, path: string, opts: { whole?: boolean; status?: string; rest?: RestFacts } = {}) {
  const said: string[] = []
  const listeners = new Set<() => void>()
  const state = { status: opts.status ?? 'live', whole: opts.whole ?? true, rest: opts.rest ?? { liveForMs: null, lastPictureMs: null, stillAfterLiveMs: null } }
  const source = {
    status: () => state.status,
    wasWhole: () => state.whole,
    restFacts: () => state.rest,
    countWhole: () => { state.whole = true; said.push('counted whole') },
    visible: (on: boolean) => { said.push(`visible ${on}`) },
    resize: () => undefined,
    subscribe: (l: () => void) => { listeners.add(l); return () => { listeners.delete(l) } },
    dispose: () => { said.push('closed') },
  } as unknown as PageSource
  const entry: WarmEntry = { id, path, css: [1000, 700], source }
  return { entry, said, set: (status: string) => { state.status = status; for (const l of [...listeners]) l() }, rest: (rest: RestFacts) => { state.rest = rest } }
}

class Channel {
  static all: Channel[] = []
  onmessage: ((e: { data: unknown }) => void) | null = null
  constructor(readonly name: string) { Channel.all.push(this) }
  postMessage(data: unknown) { for (const c of Channel.all) if (c !== this && c.name === this.name) c.onmessage?.({ data }) }
  close() { Channel.all = Channel.all.filter(c => c !== this) }
}

beforeEach(() => { Channel.all = []; vi.stubGlobal('BroadcastChannel', Channel) })
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

describe('warm rooms, as state', () => {
  it('keeps the pages of the room he leaves out of view, and hands them back when he returns', () => {
    const { result } = renderHook(() => useWarmRooms())
    const chat = page('p1', '/chat')
    const plan = page('p2', '/schedule')
    let kept = false
    act(() => { kept = result.current.leave('a', [chat.entry, plan.entry], 2) })
    expect(kept).toBe(true)
    expect(result.current.warm).toEqual(['a'])
    expect(result.current.count()).toBe(2)
    // Told that nobody sees them; not closed. The workspace does not free their sources.
    expect(chat.said).toEqual(['visible false'])
    expect(result.current.sources()).toEqual([chat.entry.source, plan.entry.source])
    // He comes back: the record keeps both, in another order. Each kept page takes its warm page.
    let got: (WarmEntry | null)[] = []
    act(() => { got = result.current.take('a', [{ path: '/schedule' }, { path: '/chat' }, { path: '/projects' }]) })
    expect(got.map(p => p?.id ?? null)).toEqual(['p2', 'p1', null])
    expect(result.current.warm).toEqual([])
    expect(chat.said).toEqual(['visible false'])
  })

  it('tells a rested room\'s pages "seen" for 10 s at most, however long the rest lasts (D53 4)', () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useWarmRooms())
    const plan = page('p2', '/schedule')
    const said: string[] = []
    const peeking = { ...plan.entry, source: { ...plan.entry.source, peek: (on: boolean) => { said.push(`peek ${on}`) } } as unknown as PageSource }
    act(() => { result.current.leave('a', [peeking], 1) })
    act(() => { result.current.rest('a', true) })
    expect(said).toEqual(['peek true'])
    act(() => { vi.advanceTimersByTime(9_900) })
    expect(said).toEqual(['peek true'])
    act(() => { vi.advanceTimersByTime(200) })
    expect(said).toEqual(['peek true', 'peek false'])
    // The dot leaves and rests again: told again.
    act(() => { result.current.rest('a', false); result.current.rest('a', true) })
    expect(said.at(-1)).toBe('peek true')
  })

  it('does not keep a room when one page was not whole or not live: it would come back as a shell', () => {
    const { result } = renderHook(() => useWarmRooms())
    const young = page('p2', '/members', { whole: false })
    let kept = true
    act(() => { kept = result.current.leave('a', [page('p1', '/chat').entry, young.entry], 1) })
    expect(kept).toBe(false)
    expect(result.current.warm).toEqual([])
    // Not told anything and not closed here: the workspace closes them as before.
    expect(young.said).toEqual([])
    act(() => { kept = result.current.leave('a', [page('p3', '/chat', { status: 'loading' }).entry], 1) })
    expect(kept).toBe(false)
    act(() => { kept = result.current.leave('a', [], 1) })
    expect(kept).toBe(false)
  })

  it('lets the record decide: a warm page that the room no longer keeps closes when he comes back', () => {
    const { result } = renderHook(() => useWarmRooms())
    const chat = page('p1', '/chat')
    const plan = page('p2', '/schedule')
    act(() => { result.current.leave('a', [chat.entry, plan.entry], 1) })
    let got: (WarmEntry | null)[] = []
    act(() => { got = result.current.take('a', [{ path: '/chat' }]) })
    expect(got.map(p => p?.id)).toEqual(['p1'])
    expect(plan.said).toEqual(['visible false', 'closed'])
    expect(chat.said).toEqual(['visible false'])
  })

  it('closes the room left longest ago when the rooms no longer fit inside ten, and gives way to a page he opens', () => {
    const { result } = renderHook(() => useWarmRooms())
    vi.useFakeTimers()
    const a = [page('a1', '/a1'), page('a2', '/a2'), page('a3', '/a3')]
    const b = [page('b1', '/b1'), page('b2', '/b2'), page('b3', '/b3'), page('b4', '/b4')]
    act(() => { result.current.leave('a', a.map(p => p.entry), 4) })
    vi.advanceTimersByTime(1000)
    // He leaves b for a room of four: 4 + 4 + 3 is eleven, so a (left first) closes.
    act(() => { result.current.leave('b', b.map(p => p.entry), 4) })
    expect(result.current.warm).toEqual(['b'])
    expect(a.every(p => p.said.at(-1) === 'closed')).toBe(true)
    // Eight stand and four are warm: a page he opens needs a place, so b closes.
    act(() => { result.current.makeRoom(8, 1) })
    expect(result.current.warm).toEqual([])
    expect(b.every(p => p.said.at(-1) === 'closed')).toBe(true)
  })

  it('does not keep a room that does not fit beside the room he goes to, and closes nothing of it', () => {
    const { result } = renderHook(() => useWarmRooms())
    const six = Array.from({ length: 6 }, (_, i) => page(`s${i}`, `/s${i}`))
    let kept = true
    act(() => { kept = result.current.leave('six', six.map(p => p.entry), 6) })
    expect(kept).toBe(false)
    expect(six.every(p => p.said.length === 0)).toBe(true)
  })

  it('makes a room cold when one of its pages is no longer live, when a page of its path opens, and when it is removed', () => {
    const { result } = renderHook(() => useWarmRooms())
    const chat = page('p1', '/chat')
    const plan = page('p2', '/schedule')
    act(() => { result.current.leave('a', [chat.entry, plan.entry], 1) })
    // The gateway closed one (or the socket went: it would load again, out of view).
    act(() => { plan.set('closed') })
    expect(result.current.warm).toEqual([])
    expect(chat.said.at(-1)).toBe('closed')
    // A page of a warm page's path is opened in the room: no page is open twice.
    const b = page('p3', '/members')
    act(() => { result.current.leave('b', [b.entry], 1) })
    act(() => { result.current.clearPath('/crons') })
    expect(result.current.warm).toEqual(['b'])
    act(() => { result.current.clearPath('/members') })
    expect(result.current.warm).toEqual([])
    expect(b.said.at(-1)).toBe('closed')
    // The room was removed (here or in another tab).
    const c = page('p4', '/sessions')
    act(() => { result.current.leave('c', [c.entry], 1) })
    act(() => { result.current.only(['a', 'b']) })
    expect(c.said.at(-1)).toBe('closed')
  })

  it('takes a page from another warm room when the room he goes to keeps it too: that room is cold, the page stays', () => {
    const { result } = renderHook(() => useWarmRooms())
    const chat = page('p1', '/chat')
    const bots = page('p2', '/members')
    act(() => { result.current.leave('b', [chat.entry, bots.entry], 1) })
    let got: (WarmEntry | null)[] = []
    act(() => { got = result.current.take('a', [{ path: '/chat' }]) })
    expect(got.map(p => p?.id)).toEqual(['p1'])
    expect(chat.said).toEqual(['visible false'])
    expect(bots.said.at(-1)).toBe('closed')
    expect(result.current.warm).toEqual([])
  })

  it('keeps no warm page while another tab of this browser shows the room, and closes its own when one comes', () => {
    const { result } = renderHook(() => useWarmRooms())
    const chat = page('p1', '/chat')
    act(() => { result.current.leave('a', [chat.entry], 1) })
    expect(result.current.warm).toEqual(['a'])
    // Another tab opens the room and says so.
    const other = new Channel(WARM_CHANNEL)
    const heard: unknown[] = []
    other.onmessage = e => heard.push(e.data)
    act(() => { other.postMessage({ what: 'here', id: 'tab-2' }) })
    expect(result.current.warm).toEqual([])
    expect(chat.said.at(-1)).toBe('closed')
    // This tab answered at once, so that the other keeps none either.
    expect(heard).toEqual([expect.objectContaining({ what: 'here' })])
    let kept = true
    act(() => { kept = result.current.leave('a', [page('p2', '/chat').entry], 1) })
    expect(kept).toBe(false)
    // The other tab goes: rooms are kept again.
    act(() => { other.postMessage({ what: 'bye', id: 'tab-2' }) })
    act(() => { kept = result.current.leave('a', [page('p3', '/chat').entry], 1) })
    expect(kept).toBe(true)
  })

  it('warms a room ahead: its pages are held out of sight until each has filled, then kept; one room at a time', () => {
    const { result } = renderHook(() => useWarmRooms())
    const a = page('h1', '/members', { whole: false })
    const b = page('h2', '/sessions', { whole: false })
    let started = false
    act(() => { started = result.current.warmAhead('crew', [a.entry, b.entry]) })
    expect(started).toBe(true)
    expect(result.current.warming).toBe('crew')
    expect(result.current.count()).toBe(2)
    // A second room waits for the first.
    act(() => { started = result.current.warmAhead('build', [page('h3', '/projects').entry]) })
    expect(started).toBe(false)
    // One page has its late still and has stood; the other shows its shell's still only: not yet.
    a.rest({ liveForMs: 9000, lastPictureMs: 300, stillAfterLiveMs: 6500 })
    b.rest({ liveForMs: 6000, lastPictureMs: 5500, stillAfterLiveMs: 500 })
    act(() => { result.current.tend(3) })
    expect(result.current.warm).toEqual([])
    expect(a.said).toEqual([])
    // Both have filled: they count as whole, are told that nobody sees them, and the room is warm.
    b.rest({ liveForMs: 9000, lastPictureMs: 2100, stillAfterLiveMs: 6900 })
    act(() => { result.current.tend(3) })
    expect(result.current.warm).toEqual(['crew'])
    expect(result.current.warming).toBeNull()
    expect(a.said).toEqual(['counted whole', 'visible false'])
    expect(result.current.count()).toBe(2)
  })

  it('gives up a room that is being warmed ahead: a page of it refused, a page he opens, a switch, too long', () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useWarmRooms())
    const a = page('h1', '/members', { whole: false })
    act(() => { result.current.warmAhead('crew', [a.entry]) })
    act(() => { a.set('failed'); result.current.tend(3) })
    expect(result.current.warming).toBeNull()
    expect(a.said).toEqual(['closed'])
    // A room that was given up is not tried again at once (it would be asked for every half minute for ever).
    expect(result.current.mayAhead('crew')).toBe(false)
    let again = true
    act(() => { again = result.current.warmAhead('crew', [page('h9', '/members').entry]) })
    expect(again).toBe(false)
    // Five minutes later (he worked in the tab meanwhile, so it is not idle) it may be tried again.
    act(() => { vi.advanceTimersByTime(299_000); window.dispatchEvent(new Event('pointermove')); vi.advanceTimersByTime(1_000) })
    expect(result.current.mayAhead('crew')).toBe(true)
    // He opens a page when the twelve are full of standing pages and this one: it goes first.
    const b = page('h2', '/members', { whole: false })
    act(() => { result.current.warmAhead('crew', [b.entry]) })
    act(() => { result.current.makeRoom(11, 1) })
    expect(b.said).toEqual(['closed'])
    // He opens a page of its path.
    const c = page('h3', '/members', { whole: false })
    act(() => { result.current.warmAhead('crew', [c.entry]) })
    act(() => { result.current.clearPath('/members') })
    expect(c.said).toEqual(['closed'])
    // He switches while it is half filled: nothing half filled is handed over.
    const d = page('h4', '/members', { whole: false })
    act(() => { result.current.warmAhead('crew', [d.entry]) })
    let got: (WarmEntry | null)[] = []
    act(() => { got = result.current.take('crew', [{ path: '/members' }]) })
    expect(got).toEqual([null])
    expect(d.said).toEqual(['closed'])
    // It never fills.
    const e = page('h5', '/members', { whole: false })
    act(() => { result.current.warmAhead('crew', [e.entry]) })
    vi.advanceTimersByTime(31_000)
    act(() => { result.current.tend(3) })
    expect(e.said).toEqual(['closed'])
    expect(result.current.warming).toBeNull()
  })

  it('keeps a second tab that is hidden for six minutes, whose beat comes once a minute: no warm page all that while (M2)', () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useWarmRooms())
    const other = new Channel(WARM_CHANNEL)
    act(() => { other.postMessage({ what: 'here', id: 'tab-2' }) })
    // Hidden for over five minutes: Chrome runs its timers once a minute, so its beat comes once a minute.
    let kept = true
    for (let minute = 1; minute <= 6; minute += 1) {
      // He works in this tab meanwhile, so it is not idle.
      act(() => { vi.advanceTimersByTime(59_000); window.dispatchEvent(new Event('keydown')) })
      act(() => { kept = result.current.leave('a', [page(`p${minute}`, '/chat').entry], 1) })
      expect(kept).toBe(false)
      expect(result.current.mayAhead('b')).toBe(false)
      act(() => { vi.advanceTimersByTime(1_000); other.postMessage({ what: 'here', id: 'tab-2' }) })
    }
    // A tab that closes says "bye": rooms are kept again at once.
    act(() => { other.postMessage({ what: 'bye', id: 'tab-2' }) })
    act(() => { kept = result.current.leave('a', [page('p9', '/chat').entry], 1) })
    expect(kept).toBe(true)
    // A tab that died with no word counts as gone after 150 s.
    expect(TAB_GONE_MS).toBe(150_000)
  })

  it('gives way to a page that the gateway refuses as one too many: the warming room first, then the oldest, then "full" (M1 a)', () => {
    const { result } = renderHook(() => useWarmRooms())
    const old = page('a1', '/chat')
    const young = page('b1', '/schedule')
    act(() => { result.current.leave('a', [old.entry], 1, 1000) })
    act(() => { result.current.leave('b', [young.entry], 1, 2000) })
    const warming = page('h1', '/members', { status: 'loading' })
    act(() => { result.current.warmAhead('c', [warming.entry]) })
    const mine = page('m1', '/settings').entry.source
    // His page is refused: the room being warmed ahead goes, and he asks again.
    let freed = false
    act(() => { freed = freeRoomFor(mine) })
    expect(freed).toBe(true)
    expect(warming.said).toEqual(['closed'])
    expect(result.current.warm).toEqual(['a', 'b'])
    // Refused again: the room he left longest ago.
    act(() => { freed = freeRoomFor(mine) })
    expect(freed).toBe(true)
    expect(old.said.at(-1)).toBe('closed')
    expect(result.current.warm).toEqual(['b'])
    act(() => { freed = freeRoomFor(mine) })
    expect(result.current.warm).toEqual([])
    // None is left: the page says "full".
    act(() => { freed = freeRoomFor(mine) })
    expect(freed).toBe(false)
    // A warm page's own refusal frees nothing for itself.
    const w = page('w1', '/chat')
    act(() => { result.current.leave('d', [w.entry], 1) })
    act(() => { freed = freeRoomFor(w.entry.source) })
    expect(freed).toBe(false)
    expect(result.current.warm).toEqual(['d'])
  })

  it('keeps no warm page in a hidden tab, and warms again once it shows (M1 b)', () => {
    vi.useFakeTimers()
    let state: DocumentVisibilityState = 'visible'
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => state)
    const { result } = renderHook(() => useWarmRooms())
    const chat = page('p1', '/chat')
    act(() => { result.current.leave('a', [chat.entry], 1) })
    const warming = page('h1', '/members', { status: 'loading' })
    act(() => { result.current.warmAhead('b', [warming.entry]) })
    state = 'hidden'
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    expect(result.current.warm).toEqual([])
    expect(chat.said.at(-1)).toBe('closed')
    expect(warming.said).toEqual(['closed'])
    expect(result.current.sleeping).toBe(true)
    let kept = true
    act(() => { kept = result.current.leave('a', [page('p2', '/chat').entry], 1) })
    expect(kept).toBe(false)
    expect(result.current.left()?.why).toBe('asleep')
    expect(result.current.mayAhead('b')).toBe(false)
    // Shown again within a second: warm ahead starts over, and the room given up a moment ago may be warmed again.
    act(() => { vi.advanceTimersByTime(500) })
    state = 'visible'
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    expect(result.current.sleeping).toBe(false)
    expect(result.current.mayAhead('b')).toBe(true)
    act(() => { kept = result.current.leave('a', [page('p3', '/chat').entry], 1) })
    expect(kept).toBe(true)
  })

  it('lets the warm rooms go after five minutes with nothing done outside a session, and warms again at his first input (M1 c)', () => {
    vi.useFakeTimers()
    let session = false
    const { result } = renderHook(() => useWarmRooms({ inSession: () => session }))
    const chat = page('p1', '/chat')
    act(() => { result.current.leave('a', [chat.entry], 1) })
    act(() => { vi.advanceTimersByTime(IDLE_MS - 2000) })
    expect(result.current.warm).toEqual(['a'])
    act(() => { vi.advanceTimersByTime(3000) })
    expect(result.current.warm).toEqual([])
    expect(result.current.sleeping).toBe(true)
    // His first input wakes it.
    act(() => { window.dispatchEvent(new Event('pointermove')) })
    expect(result.current.sleeping).toBe(false)
    expect(result.current.mayAhead('b')).toBe(true)
    // In a session he may read for long with his hands still: that is not "nothing done".
    session = true
    act(() => { result.current.leave('a', [page('p2', '/chat').entry], 1) })
    act(() => { vi.advanceTimersByTime(IDLE_MS * 2) })
    expect(result.current.warm).toEqual(['a'])
  })

  it('stamps a room warmed ahead as left before any room he left: his rooms outlive it (small 1)', () => {
    const { result } = renderHook(() => useWarmRooms())
    const left = page('l1', '/chat')
    act(() => { result.current.leave('b', [left.entry], 1) })
    const d = page('d1', '/members', { rest: { liveForMs: 16_000, lastPictureMs: 100, stillAfterLiveMs: 7000 } })
    act(() => { result.current.warmAhead('d', [d.entry]) })
    act(() => { result.current.tend(1) })
    expect(result.current.warm).toEqual(['b', 'd'])
    // One room must give way: the one warmed ahead, not the one he left.
    act(() => { result.current.makeRoom(10, 1) })
    expect(result.current.warm).toEqual(['b'])
  })

  it('makes a warm room cold when a switch to it could no longer be served by the warm pages (small 3)', () => {
    const { result } = renderHook(() => useWarmRooms())
    act(() => { result.current.leave('a', [page('a1', '/schedule').entry], 1, 1000) })
    act(() => { result.current.leave('b', [page('b1', '/chat').entry], 1, 2000) })
    // Room a needs its schedule and a chat: the chat is warm in b, so a switch to a is quick.
    act(() => { result.current.prune(room => (room === 'a' ? [{ path: '/schedule' }, { path: '/chat' }] : [{ path: '/chat' }])) })
    expect(result.current.warm).toEqual(['a', 'b'])
    // b closes: a would ask for a chat afresh, so its dot goes out too.
    act(() => { result.current.only(['a']) })
    act(() => { result.current.prune(() => [{ path: '/schedule' }, { path: '/chat' }]) })
    expect(result.current.warm).toEqual([])
  })

  it('closes every warm page when the workspace is left', () => {
    const { result, unmount } = renderHook(() => useWarmRooms())
    const chat = page('p1', '/chat')
    act(() => { result.current.leave('a', [chat.entry], 1) })
    unmount()
    expect(chat.said.at(-1)).toBe('closed')
  })
})
