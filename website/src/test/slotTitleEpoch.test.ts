import { describe, it, expect } from 'vitest'
import reducer, { sseSlots, sseSlotTitle } from '../store/dashboardSlice'
import type { ChatSlot } from '../store/dashboardSlice'

function slot(key: string, title: string): ChatSlot {
  return { key, title, status: 'idle' } as unknown as ChatSlot
}

function seeded() {
  return reducer(undefined, sseSlots([slot('chat-a', 'Original')]))
}

describe('slot title ordering under a timed-out rename', () => {
  it('keeps the winning rename when the aborted one reports in late', () => {
    let state = seeded()
    // Rename A is stamped first, then times out client-side; B is stamped after
    // and lands. A's persistence finishes last and announces the name it lost with.
    state = reducer(state, sseSlotTitle({ key: 'chat-a', title: 'B wins', epoch: 8 }))
    state = reducer(state, sseSlotTitle({ key: 'chat-a', title: 'A was aborted', epoch: 7 }))
    expect(state.slots.find(s => s.key === 'chat-a')?.title).toBe('B wins')
  })

  it('does not count a discarded frame as the server having spoken', () => {
    let state = seeded()
    state = reducer(state, sseSlotTitle({ key: 'chat-a', title: 'B wins', epoch: 8 }))
    const after = state.slotTitleGenerations['chat-a']
    state = reducer(state, sseSlotTitle({ key: 'chat-a', title: 'A was aborted', epoch: 7 }))
    expect(state.slotTitleGenerations['chat-a']).toBe(after)
  })

  it('applies a newer epoch', () => {
    let state = seeded()
    state = reducer(state, sseSlotTitle({ key: 'chat-a', title: 'First', epoch: 7 }))
    state = reducer(state, sseSlotTitle({ key: 'chat-a', title: 'Second', epoch: 8 }))
    expect(state.slots.find(s => s.key === 'chat-a')?.title).toBe('Second')
  })

  it('still applies an unstamped title, which orders against nothing', () => {
    let state = seeded()
    state = reducer(state, sseSlotTitle({ key: 'chat-a', title: 'Stamped', epoch: 8 }))
    state = reducer(state, sseSlotTitle({ key: 'chat-a', title: 'Background generated' }))
    expect(state.slots.find(s => s.key === 'chat-a')?.title).toBe('Background generated')
  })
})
