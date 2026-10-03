import { describe, it, expect, vi } from 'vitest'
import reducer, { sseSlots, sseSlotPatch, sseConnected, addSlotOptimistic, fetchSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({
  api: { chatSlots: vi.fn(), chatMode: vi.fn() },
}))

/**
 * Membership races between a `/api/chat/slots` reply and the live stream.
 *
 * The scenario that surfaced them: an agent files sessions into a large folder
 * through the dashboard MCP (`session_create`), which marks each new row
 * `lineage_pending` and makes the sidebar poll `fetchSlots` every 2-8 s, while
 * the person creates a session in the same folder by hand. Every reply that was
 * serialized before a row joined the list used to take that row away again, and
 * the next live frame put it back: the row blinked out and the whole folder
 * re-flowed around the gap, once per poll. The reverse held for a row the
 * stream removed while a reply that still listed it was travelling.
 *
 * The reply is still authoritative for everything it DID see; these cases pin
 * only the rows it could not have seen.
 */

const mk = (key: string, over: Partial<ChatSlot> = {}): ChatSlot => ({
  key,
  title: key,
  messages: 1,
  running: false,
  pending_approval: false,
  waiting_for_input: false,
  last_activity_ts: undefined,
  ...over,
})

const wire = (...slots: ChatSlot[]): ChatSlot[] => slots.map(s => JSON.parse(JSON.stringify(s)) as ChatSlot)
const started = (requestId: string) => ({ type: fetchSlots.pending.type, meta: { requestId, requestStatus: 'pending' } })
const reply = (slots: ChatSlot[], requestId: string) => ({
  type: fetchSlots.fulfilled.type,
  payload: wire(...slots),
  meta: { requestId, requestStatus: 'fulfilled' },
})
const created = (slot: ChatSlot) => ({ type: 'chat/createSlot/fulfilled', payload: slot, meta: { requestId: 'c', requestStatus: 'fulfilled' } })
const loaded = (...slots: ChatSlot[]) => reducer(reducer(undefined, { type: '@@INIT' }), sseSlots(wire(...slots)))
const keys = (state: ReturnType<typeof loaded>) => state.slots.map(s => s.key)

describe('a fetchSlots reply cannot take away a row it never saw', () => {
  it('keeps a row a live frame added while the reply travelled', () => {
    let s = loaded(mk('a', { folder_id: 'f' }), mk('b', { folder_id: 'f' }))
    s = reducer(s, started('r1'))
    // The agent's session_create broadcast lands mid-flight.
    s = reducer(s, sseSlots(wire(mk('a', { folder_id: 'f' }), mk('new', { folder_id: 'f' }), mk('b', { folder_id: 'f' }))))
    s = reducer(s, reply([mk('a', { folder_id: 'f' }), mk('b', { folder_id: 'f' })], 'r1'))
    expect(keys(s)).toEqual(['a', 'new', 'b'])
  })

  it('keeps the row the person just created', () => {
    let s = loaded(mk('a'), mk('b'))
    s = reducer(s, started('r1'))
    s = reducer(s, created(mk('mine', { folder_id: 'f' })))
    s = reducer(s, reply([mk('a'), mk('b')], 'r1'))
    expect(keys(s)).toContain('mine')
  })

  it('keeps a row added by addSlotOptimistic (resume/fork)', () => {
    let s = loaded(mk('a'))
    s = reducer(s, started('r1'))
    s = reducer(s, addSlotOptimistic(mk('fork')))
    s = reducer(s, reply([mk('a')], 'r1'))
    expect(keys(s)).toEqual(['a', 'fork'])
  })

  it('keeps the row object identical so the sidebar does not re-measure it', () => {
    let s = loaded(mk('a'))
    s = reducer(s, started('r1'))
    s = reducer(s, sseSlots(wire(mk('a'), mk('new'))))
    const before = s.slots.find(r => r.key === 'new')
    s = reducer(s, reply([mk('a')], 'r1'))
    expect(s.slots.find(r => r.key === 'new')).toBe(before)
  })

  it('still drops a row that existed before the request and is absent from the reply', () => {
    let s = loaded(mk('a'), mk('gone'))
    s = reducer(s, started('r1'))
    s = reducer(s, reply([mk('a')], 'r1'))
    expect(keys(s)).toEqual(['a'])
  })

  it('does not protect a row added before the request was dispatched', () => {
    let s = loaded(mk('a'))
    s = reducer(s, sseSlots(wire(mk('a'), mk('early'))))
    s = reducer(s, started('r1'))
    s = reducer(s, reply([mk('a')], 'r1'))
    expect(keys(s)).toEqual(['a'])
  })

  it('drops a row introduced only by a newer overlapping HTTP reply', () => {
    let s = loaded(mk('a'))
    s = reducer(s, started('older'))
    s = reducer(s, started('newer'))
    s = reducer(s, reply([mk('a'), mk('http-only')], 'newer'))
    expect(keys(s)).toEqual(['a', 'http-only'])
    s = reducer(s, reply([mk('a')], 'older'))
    expect(keys(s)).toEqual(['a'])
  })

  it('survives many overlapping polls while an agent keeps adding rows', () => {
    let s = loaded(...Array.from({ length: 100 }, (_, i) => mk(`s${i}`, { folder_id: 'f' })))
    const base = s.slots.map(r => ({ ...r }))
    const live = [...base]
    for (let n = 0; n < 10; n++) {
      s = reducer(s, started(`r${n}`))
      live.unshift(mk(`agent${n}`, { folder_id: 'f' }))
      s = reducer(s, sseSlots(wire(...live)))
      // The reply was serialized before this iteration's row existed.
      s = reducer(s, reply(live.slice(1), `r${n}`))
      expect(keys(s)).toEqual(live.map(r => r.key))
    }
  })
})

describe('a fetchSlots reply cannot restore a row the stream removed', () => {
  it('a full live frame that omits a row marks in-flight replies stale for it', () => {
    let s = loaded(mk('a'), mk('b'))
    s = reducer(s, started('r1'))
    s = reducer(s, sseSlots(wire(mk('a'))))
    s = reducer(s, reply([mk('a'), mk('b')], 'r1'))
    expect(keys(s)).toEqual(['a'])
  })

  it('fences the reconnect catch-up fetch against a row the first live frame omits', () => {
    let s = loaded(mk('a'), mk('b'))
    // Reconnect: the flag drops, the rows stay, the catch-up fetch goes out.
    s = reducer(s, sseConnected())
    s = reducer(s, started('r1'))
    s = reducer(s, sseSlots(wire(mk('a'))))
    s = reducer(s, reply([mk('a'), mk('b')], 'r1'))
    expect(keys(s)).toEqual(['a'])
  })

  it('keeps a row recreated under a removed key after the reply was requested', () => {
    // r1 goes out, the stream removes k (fencing r1 for k), the server builds
    // r1 without k, then k is resumed here. r1 cannot have seen the new k.
    for (const remove of [
      (st: ReturnType<typeof loaded>) => reducer(st, sseSlotPatch({ removed: ['k'] })),
      (st: ReturnType<typeof loaded>) => reducer(st, sseSlots(wire(mk('a'), mk('b')))),
    ]) {
      let s = loaded(mk('a'), mk('k'), mk('b'))
      s = reducer(s, started('r1'))
      s = remove(s)
      s = reducer(s, addSlotOptimistic(mk('k')))
      s = reducer(s, reply([mk('a'), mk('b')], 'r1'))
      expect(keys(s)).toContain('k')
    }
  })

  it('a slot_patch removal is still honoured (regression guard)', () => {
    let s = loaded(mk('a'), mk('b'))
    s = reducer(s, started('r1'))
    s = reducer(s, sseSlotPatch({ removed: ['b'] }))
    s = reducer(s, reply([mk('a'), mk('b')], 'r1'))
    expect(keys(s)).toEqual(['a'])
  })
})

describe('a full live frame older than a just-created row does not fence the reply that lists it', () => {
  // createBoundSession / startSession: the POST resolves, `addSlotOptimistic(K)`
  // pushes the row and the refresh `fetchSlots` goes out. A full frame the
  // server serialized just before its `put_slot`, delivered after the response,
  // omits K -- but no live frame has ever listed K, so the omission is not a
  // removal and the reply, which does list K, must be allowed to put it back.
  it('keeps an addSlotOptimistic row the refresh reply lists after an older frame omitted it', () => {
    let s = loaded(mk('a'))
    s = reducer(s, addSlotOptimistic(mk('k')))
    s = reducer(s, started('refresh'))
    s = reducer(s, sseSlots(wire(mk('a'))))
    s = reducer(s, reply([mk('a'), mk('k')], 'refresh'))
    expect(keys(s)).toEqual(['a', 'k'])
  })

  it('same for a row the person created through the create thunk', () => {
    let s = loaded(mk('a'))
    s = reducer(s, created(mk('mine')))
    s = reducer(s, started('refresh'))
    s = reducer(s, sseSlots(wire(mk('a'))))
    s = reducer(s, reply([mk('a'), mk('mine')], 'refresh'))
    expect(keys(s)).toEqual(['a', 'mine'])
  })

  it('still fences the key once a live frame has listed it', () => {
    let s = loaded(mk('a'))
    s = reducer(s, addSlotOptimistic(mk('k')))
    s = reducer(s, sseSlots(wire(mk('a'), mk('k'))))
    s = reducer(s, started('r1'))
    // Ordered after the frame that listed k: a real removal.
    s = reducer(s, sseSlots(wire(mk('a'))))
    s = reducer(s, reply([mk('a'), mk('k')], 'r1'))
    expect(keys(s)).toEqual(['a'])
  })

  it('a key removed by an HTTP reply and resumed under the same key starts unlisted again', () => {
    let s = loaded(mk('a'), mk('k'))
    s = reducer(s, started('removal'))
    s = reducer(s, reply([mk('a')], 'removal'))
    s = reducer(s, addSlotOptimistic(mk('k')))
    s = reducer(s, started('refresh'))
    // Serialized before the resume registered k, so this omission is older.
    s = reducer(s, sseSlots(wire(mk('a'))))
    s = reducer(s, reply([mk('a'), mk('k')], 'refresh'))
    expect(keys(s)).toEqual(['a', 'k'])
  })

  it('a key removed by slot_patch and resumed under the same key starts unlisted again', () => {
    let s = loaded(mk('a'), mk('k'))
    s = reducer(s, sseSlotPatch({ removed: ['k'] }))
    s = reducer(s, addSlotOptimistic(mk('k')))
    s = reducer(s, started('refresh'))
    s = reducer(s, sseSlots(wire(mk('a'))))
    s = reducer(s, reply([mk('a'), mk('k')], 'refresh'))
    expect(keys(s)).toEqual(['a', 'k'])
  })
})

describe('an HTTP removal clears an add stamp even after the stream is live', () => {
  it('does not restore a server-removed row after a stale reply re-lists it', () => {
    let s = loaded(mk('a'))
    s = reducer(s, started('older-omission'))
    s = reducer(s, addSlotOptimistic(mk('k')))
    s = reducer(s, started('stale-relist'))
    s = reducer(s, started('authoritative-removal'))

    s = reducer(s, reply([mk('a')], 'authoritative-removal'))
    expect(keys(s)).toEqual(['a'])
    s = reducer(s, reply([mk('a'), mk('k')], 'stale-relist'))
    expect(keys(s)).toEqual(['a', 'k'])
    s = reducer(s, reply([mk('a')], 'older-omission'))

    expect(keys(s)).toEqual(['a'])
  })
})
