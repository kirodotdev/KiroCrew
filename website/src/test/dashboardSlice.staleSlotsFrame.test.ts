import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import reducer, {
  sseSlots,
  sseSlotPatch,
  addSlotOptimistic,
  removeSlotOptimistic,
  fetchSlots,
  readUnlistedMembers,
  omitsUnlistedMember,
} from '../store/dashboardSlice'
import { api } from '../api/client'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({
  api: { chatSlots: vi.fn(), chatMode: vi.fn() },
}))

/**
 * A full `slots` WebSocket frame carries no ordering mark. One the gateway
 * serialized before a create or fork can reach the tab after the op's HTTP
 * response, omitting the new key. Such a frame may predate a local membership
 * add, so it cannot remove that member on its own: applied as a whole-list
 * replace, it would drop the row the tab just opened, and the chat page would
 * clear the active session. Only a later list read settles membership.
 *
 * These cases pin both halves of that rule: the row survives a frame that may
 * predate it, and a chat deleted elsewhere is not kept past one list read.
 */

const mk = (key: string): ChatSlot => ({
  key, title: key, messages: 2, running: false, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined,
})
const wire = (...keys: string[]): ChatSlot[] => keys.map(k => JSON.parse(JSON.stringify(mk(k))) as ChatSlot)
const keys = (s: { slots: ChatSlot[] }) => s.slots.map(r => r.key)
const created = (key: string) => ({ type: 'chat/createSlot/fulfilled', payload: mk(key), meta: { requestId: 'c1', requestStatus: 'fulfilled' } })
const started = (requestId: string) => ({ type: fetchSlots.pending.type, meta: { requestId, requestStatus: 'pending' } })
const replied = (requestId: string, ...k: string[]) => ({ type: fetchSlots.fulfilled.type, payload: wire(...k), meta: { requestId, requestStatus: 'fulfilled' } })
const failed = (requestId: string) => ({ type: fetchSlots.rejected.type, error: { message: 'x' }, meta: { requestId, requestStatus: 'rejected' } })

const loaded = () => reducer(reducer(undefined, { type: '@@INIT' }), sseSlots(wire('src', 'other')))

afterEach(() => { vi.useRealTimers() })

describe('a full slots frame that predates a create or fork', () => {
  it('keeps a forked child the frame omits, and asks for a list read', () => {
    let s = reducer(loaded(), addSlotOptimistic(mk('child')))
    s = reducer(s, sseSlots(wire('src', 'other')))
    expect(keys(s)).toEqual(['src', 'other', 'child'])
    expect(s.slotMemberReadWanted).toBe(true)
  })

  it('keeps a created chat the frame omits', () => {
    let s = reducer(loaded(), created('new'))
    s = reducer(s, sseSlots(wire('src', 'other')))
    expect(keys(s)).toContain('new')
  })

  it('asks for no second read while the first is pending', () => {
    let s = reducer(loaded(), addSlotOptimistic(mk('child')))
    s = reducer(s, sseSlots(wire('src', 'other')))
    s = reducer(s, started('r1'))
    expect(s.slotMemberReadWanted).toBe(false)
    s = reducer(s, sseSlots(wire('src', 'other')))
    expect(s.slotMemberReadWanted).toBe(false)
    expect(keys(s)).toContain('child')
  })

  it('keeps the key through an older frame that lands after one listing it', () => {
    let s = reducer(loaded(), addSlotOptimistic(mk('child')))
    s = reducer(s, sseSlots(wire('src', 'other', 'child')))
    expect(s.slotMemberReadWanted ?? false).toBe(false)
    s = reducer(s, sseSlots(wire('src', 'other')))
    expect(keys(s)).toContain('child')
    expect(s.slotMemberReadWanted).toBe(true)
  })

  it('applies frames as before once the time cap has passed', () => {
    vi.useFakeTimers()
    vi.setSystemTime(2_000_000)
    let s = reducer(loaded(), addSlotOptimistic(mk('child')))
    s = reducer(s, sseSlots(wire('src', 'other', 'child')))
    vi.setSystemTime(2_000_000 + 15_001)
    s = reducer(s, sseSlots(wire('src', 'other')))
    expect(keys(s)).toEqual(['src', 'other'])
    expect(s.slotMemberUnlisted).toEqual({})
  })

  it('drops the key when a list read sent after the add omits it (deleted elsewhere)', () => {
    let s = reducer(loaded(), addSlotOptimistic(mk('child')))
    s = reducer(s, sseSlots(wire('src', 'other')))
    s = reducer(s, started('r1'))
    s = reducer(s, replied('r1', 'src', 'other'))
    expect(keys(s)).toEqual(['src', 'other'])
    // A further stale frame cannot bring it back.
    s = reducer(s, sseSlots(wire('src', 'other')))
    expect(keys(s)).toEqual(['src', 'other'])
  })

  it('keeps a read sent before the add from reviving a key a later read dropped', () => {
    let s = reducer(loaded(), started('r0'))
    s = reducer(s, addSlotOptimistic(mk('child')))
    s = reducer(s, sseSlots(wire('src', 'other')))
    s = reducer(s, started('r1'))
    s = reducer(s, replied('r1', 'src', 'other'))
    s = reducer(s, replied('r0', 'src', 'other'))
    expect(keys(s)).toEqual(['src', 'other'])
  })

  it('keeps the key through a stale frame that lands after a read listing it, and asks again', () => {
    let s = reducer(loaded(), addSlotOptimistic(mk('child')))
    s = reducer(s, sseSlots(wire('src', 'other')))
    s = reducer(s, started('r1'))
    s = reducer(s, replied('r1', 'src', 'other', 'child'))
    expect(keys(s)).toContain('child')
    s = reducer(s, sseSlots(wire('src', 'other')))
    expect(keys(s)).toContain('child')
    expect(s.slotMemberReadWanted).toBe(true)
  })

  it('does not let a list read sent BEFORE the add end the retention', () => {
    let s = reducer(loaded(), started('r0'))
    s = reducer(s, addSlotOptimistic(mk('child')))
    s = reducer(s, replied('r0', 'src', 'other'))
    expect(keys(s)).toContain('child')
    s = reducer(s, sseSlots(wire('src', 'other')))
    expect(keys(s)).toContain('child')
  })

  it('asks again on the next stale frame when the read fails', () => {
    let s = reducer(loaded(), addSlotOptimistic(mk('child')))
    s = reducer(s, sseSlots(wire('src', 'other')))
    s = reducer(s, started('r1'))
    s = reducer(s, failed('r1'))
    s = reducer(s, sseSlots(wire('src', 'other')))
    expect(s.slotMemberReadWanted).toBe(true)
    expect(keys(s)).toContain('child')
  })

  it('stops keeping the key after the time cap', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_000_000)
    let s = reducer(loaded(), addSlotOptimistic(mk('child')))
    vi.setSystemTime(1_000_000 + 15_001)
    s = reducer(s, sseSlots(wire('src', 'other')))
    expect(keys(s)).toEqual(['src', 'other'])
  })

  it('does not keep a key this tab closed', () => {
    let s = reducer(loaded(), addSlotOptimistic(mk('child')))
    s = reducer(s, removeSlotOptimistic('child'))
    s = reducer(s, sseSlots(wire('src', 'other')))
    expect(keys(s)).toEqual(['src', 'other'])
  })

  it('does not keep a key the server reported removed', () => {
    let s = reducer(loaded(), addSlotOptimistic(mk('child')))
    s = reducer(s, sseSlotPatch({ removed: ['child'] }))
    s = reducer(s, sseSlots(wire('src', 'other')))
    expect(keys(s)).toEqual(['src', 'other'])
  })
})

describe('readUnlistedMembers', () => {
  beforeEach(() => { vi.mocked(api.chatSlots).mockReset() })
  const storeWith = () => {
    const store = configureStore({ reducer: { dashboard: reducer } })
    store.dispatch(sseSlots(wire('src', 'other')))
    return store
  }
  const frame = (store: ReturnType<typeof storeWith>, data: ChatSlot[]) => {
    store.dispatch(sseSlots(data))
    store.dispatch(readUnlistedMembers() as never)
  }

  it('reads the list once for a run of stale frames, and the reply settles the row', async () => {
    vi.mocked(api.chatSlots).mockResolvedValue(wire('src', 'other', 'child'))
    const store = storeWith()
    store.dispatch(addSlotOptimistic(mk('child')))
    frame(store, wire('src', 'other'))
    frame(store, wire('src', 'other'))
    expect(api.chatSlots).toHaveBeenCalledTimes(1)
    await vi.waitFor(() => expect(store.getState().dashboard.slotFetchesInFlight).toEqual([]))
    expect(keys(store.getState().dashboard)).toContain('child')
  })

  it('sends no read for an ordinary frame', () => {
    const store = storeWith()
    frame(store, wire('src', 'other', 'x'))
    expect(api.chatSlots).not.toHaveBeenCalled()
  })
})

describe('omitsUnlistedMember (the frame handler skips repeat suppression on it)', () => {
  it('is false with nothing kept, and for a frame that lists every kept key', () => {
    expect(omitsUnlistedMember(loaded(), wire('src', 'other'))).toBe(false)
    const s = reducer(loaded(), addSlotOptimistic(mk('child')))
    expect(omitsUnlistedMember(s, wire('src', 'other', 'child'))).toBe(false)
    expect(omitsUnlistedMember(s, null)).toBe(false)
  })

  it('stays true for an identical repeat after a failed read, so the repeat retries the read', () => {
    let s = reducer(loaded(), addSlotOptimistic(mk('child')))
    s = reducer(s, sseSlots(wire('src', 'other')))
    s = reducer(s, started('r1'))
    s = reducer(s, failed('r1'))
    expect(omitsUnlistedMember(s, wire('src', 'other'))).toBe(true)
    s = reducer(s, sseSlots(wire('src', 'other')))
    expect(s.slotMemberReadWanted).toBe(true)
  })

  it('lets an identical repeat past the 15 s cap drop the row and end the bypass', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_000_000)
    let s = reducer(loaded(), addSlotOptimistic(mk('child')))
    s = reducer(s, sseSlots(wire('src', 'other')))
    s = reducer(s, started('r1'))
    s = reducer(s, failed('r1'))
    vi.setSystemTime(1_000_000 + 15_001)
    expect(omitsUnlistedMember(s, wire('src', 'other'))).toBe(true)
    s = reducer(s, sseSlots(wire('src', 'other')))
    expect(keys(s)).toEqual(['src', 'other'])
    expect(omitsUnlistedMember(s, wire('src', 'other'))).toBe(false)
  })
})
