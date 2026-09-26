/**
 * `slot_patch` frames: the gateway's one-row answer to a pin, rename, folder
 * move or close, sent instead of the full slot list to a tab that declared the
 * capability. The reducer must leave the sidebar exactly as the full list
 * would have, while touching only the named row.
 */
import { describe, it, expect, vi } from 'vitest'
import reducer, {
  sseSlots,
  sseSlotPatch,
  markSlotUnread,
  sseSubagentStatus,
  fetchSlots,
  removeSlotOptimistic,
  armConfirmedCloseHold,
  addSlotOptimistic,
} from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({
  api: { chatSlots: vi.fn(), chatMode: vi.fn() },
}))

const row = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({
  key, title: key, messages: 1, running: false, folder_id: '', pinned: false, ...extra,
})

const loaded = () => reducer(undefined, sseSlots([row('a'), row('b'), row('c')]))

describe('sseSlotPatch', () => {
  it('merges only the named fields into the named row', () => {
    const before = loaded()
    const after = reducer(before, sseSlotPatch({ slots: [{ key: 'b', pinned: true, title: 'Bee' }] }))
    const b = after.slots.find(s => s.key === 'b')!
    expect(b.pinned).toBe(true)
    expect(b.title).toBe('Bee')
    expect(b.messages).toBe(1)
    // Untouched rows keep their identity, so no other sidebar row re-renders.
    expect(after.slots[0]).toBe(before.slots[0])
    expect(after.slots[2]).toBe(before.slots[2])
    // Not a full snapshot: the pin reconciler's generation stays put.
    expect(after.slotsGeneration).toBe(before.slotsGeneration)
  })

  it('leaves an equal row alone', () => {
    const before = loaded()
    const after = reducer(before, sseSlotPatch({ slots: [{ key: 'a', pinned: false }] }))
    expect(after.slots).toBe(before.slots)
    expect(after.slotWriteSeq).toBe(before.slotWriteSeq)
  })

  it('drops a row for a key this tab does not hold', () => {
    const before = loaded()
    const after = reducer(before, sseSlotPatch({ slots: [{ key: 'zzz', pinned: true }] }))
    expect(after.slots).toBe(before.slots)
  })

  it('protects the patched value from an HTTP list that was already in flight', () => {
    let state = loaded()
    state = reducer(state, fetchSlots.pending('req-1', undefined))
    state = reducer(state, sseSlotPatch({ slots: [{ key: 'a', pinned: true }] }))
    // The reply was serialized before the pin and still says unpinned.
    state = reducer(state, fetchSlots.fulfilled([row('a'), row('b'), row('c')], 'req-1', undefined))
    expect(state.slots.find(s => s.key === 'a')!.pinned).toBe(true)
  })

  it('protects a remote removal from an HTTP list that was already in flight', () => {
    let state = loaded()
    state = reducer(state, fetchSlots.pending('req-1', undefined))
    state = reducer(state, sseSlotPatch({ slots: [], removed: ['b'] }))
    state = reducer(state, fetchSlots.fulfilled([row('a'), row('b'), row('c')], 'req-1', undefined))
    expect(state.slots.map(s => s.key)).toEqual(['a', 'c'])

    state = reducer(state, addSlotOptimistic(row('b')))
    expect(state.slots.map(s => s.key)).toEqual(['a', 'c', 'b'])
  })

  it('removes a closed key and tears down its per-slot state like a full list', () => {
    let state = loaded()
    state = reducer(state, markSlotUnread('b'))
    state = reducer(state, sseSubagentStatus({ slot: 'b', running: 1 } as never))
    const before = state
    state = reducer(state, sseSlotPatch({ slots: [], removed: ['b'] }))
    expect(state.slots.map(s => s.key)).toEqual(['a', 'c'])
    expect(state.slots[0]).toBe(before.slots[0])
    expect(state.unreadSlots).not.toContain('b')
    expect(state.subagentRunning.b).toBeUndefined()
  })

  it('re-states orphaned parents carried beside a removal', () => {
    let state = reducer(undefined, sseSlots([
      row('conductor'),
      row('worker', { parent: { slot: 'conductor', key: 'conductor' } } as Partial<ChatSlot>),
    ]))
    state = reducer(state, sseSlotPatch({
      slots: [{ key: 'worker', parent: { slot: 'conductor', key: null } } as never],
      removed: ['conductor'],
    }))
    expect(state.slots.map(s => s.key)).toEqual(['worker'])
    expect((state.slots[0] as unknown as { parent: unknown }).parent).toEqual({ slot: 'conductor', key: null })
  })

  it('keeps a close tombstone holding a key a later list still names', () => {
    let state = loaded()
    state = reducer(state, armConfirmedCloseHold('b'))
    state = reducer(state, removeSlotOptimistic('b'))
    state = reducer(state, sseSlotPatch({ slots: [], removed: ['b'] }))
    // A list serialized before the pop arrives afterwards; the row stays gone.
    state = reducer(state, sseSlots([row('a'), row('b'), row('c')]))
    expect(state.slots.map(s => s.key)).not.toContain('b')
  })

  it('tears down only the removed key while another close is still in flight', () => {
    let state = loaded()
    // A's DELETE is in flight: `pending` arms the hold, the thunk body drops the
    // row. The server still runs A, so its sub-agent and unread frames keep coming.
    state = reducer(state, { type: 'chat/deleteSlot/pending', meta: { arg: 'a', requestId: 'del-a' } })
    state = reducer(state, removeSlotOptimistic('a'))
    state = reducer(state, sseSubagentStatus({ slot: 'a', running: 1 } as never))
    state = reducer(state, markSlotUnread({ slot: 'a', ts: '2026-01-01T00:00:00Z' }))
    state = reducer(state, sseSubagentStatus({ slot: 'b', running: 1 } as never))
    state = reducer(state, markSlotUnread({ slot: 'b', ts: '2026-01-01T00:00:00Z' }))
    state = reducer(state, sseSlotPatch({ slots: [], removed: ['b'] }))
    expect(state.slots.map(s => s.key)).toEqual(['c'])
    // B is gone for good; A's DELETE may still fail and bring the row back.
    expect(state.subagentRunning.b).toBeUndefined()
    expect(state.unreadSlots).not.toContain('b')
    expect(state.subagentRunning.a).toBe(1)
    expect(state.unreadSlots).toContain('a')
    expect(state.unreadSince?.a).toBe('2026-01-01T00:00:00Z')
  })

  it('ignores a removal before the first snapshot', () => {
    const initial = reducer(undefined, { type: '@@INIT' })
    const after = reducer(initial, sseSlotPatch({ slots: [], removed: ['a'] }))
    expect(after.slotsLoaded).toBe(initial.slotsLoaded)
    expect(after.slots).toBe(initial.slots)
  })
})

describe('chatSlice on sseSlotPatch', () => {
  it('retires a folder suggestion when the patch files the slot', () => {
    const initial = chatReducer(undefined, { type: '@@INIT' })
    const withSuggestion = { ...initial, folderSuggestions: { a: { folderId: 'f', ts: 1 } } } as typeof initial
    const after = chatReducer(withSuggestion, sseSlotPatch({ slots: [{ key: 'a', folder_id: 'f' }] }))
    expect(after.folderSuggestions?.a).toBeUndefined()
  })

  it('evicts a removed slot residue but never the active slot', () => {
    const initial = chatReducer(undefined, { type: '@@INIT' })
    const seeded = {
      ...initial,
      activeSlot: 'keep',
      slotMessages: { gone: [], keep: [] },
    } as unknown as typeof initial
    const after = chatReducer(seeded, sseSlotPatch({ slots: [], removed: ['gone', 'keep'] }))
    const messages = (after as unknown as { slotMessages: Record<string, unknown> }).slotMessages
    expect(messages.gone).toBeUndefined()
    expect(messages.keep).toBeDefined()
  })
})
