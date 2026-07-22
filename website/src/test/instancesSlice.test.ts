import { describe, it, expect } from 'vitest'
import reducer, {
  setWarm,
  setActiveId,
  removeWarm,
  setUnread,
  clearInstances,
} from '../store/instancesSlice'

const initial = { warm: {}, activeId: null, mru: [], unread: {}, host: null }

describe('instancesSlice', () => {
  it('setWarm stores the conn and fronts it in mru', () => {
    let s = reducer(initial, setWarm({ id: 'a', conn: { port: 7778, token: 't1' } }))
    s = reducer(s, setWarm({ id: 'b', conn: { port: 7779, token: 't2' } }))
    expect(s.warm).toEqual({ a: { port: 7778, token: 't1' }, b: { port: 7779, token: 't2' } })
    expect(s.mru).toEqual(['b', 'a'])
  })

  it('setActiveId fronts mru and clears that instance unread', () => {
    const start = { warm: {}, activeId: null, mru: ['a'], unread: { b: 3 } }
    const s = reducer(start, setActiveId('b'))
    expect(s.activeId).toBe('b')
    expect(s.mru).toEqual(['b', 'a'])
    expect(s.unread.b).toBe(0)
  })

  it('setActiveId(null) returns to Local without touching mru/unread', () => {
    const start = { warm: {}, activeId: 'b', mru: ['b', 'a'], unread: { b: 2 } }
    const s = reducer(start, setActiveId(null))
    expect(s.activeId).toBeNull()
    expect(s.mru).toEqual(['b', 'a'])
    expect(s.unread.b).toBe(2)
  })

  it('removeWarm drops warm/unread/mru and clears active if it was the victim', () => {
    const start = {
      warm: { a: { port: 1, token: 'x' } },
      activeId: 'a',
      mru: ['a'],
      unread: { a: 5 },
    }
    const s = reducer(start, removeWarm('a'))
    expect(s.warm).toEqual({})
    expect(s.unread).toEqual({})
    expect(s.mru).toEqual([])
    expect(s.activeId).toBeNull()
  })

  it('setUnread records the count and clearInstances resets', () => {
    let s = reducer(initial, setUnread({ id: 'a', count: 4 }))
    expect(s.unread.a).toBe(4)
    s = reducer(s, clearInstances())
    expect(s).toEqual(initial)
  })
})
