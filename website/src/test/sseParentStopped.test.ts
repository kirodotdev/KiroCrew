import { describe, it, expect } from 'vitest'
import reducer, { sseParentStopped, sseSubagentSpawn, setActiveSlot } from '../store/chatSlice'

// Get properly initialized state through the reducer
const initial = reducer(undefined, { type: '@@INIT' })

describe('sseParentStopped reducer', () => {
  it('marks active subagents as parentStopped', () => {
    let state = reducer(initial, sseSubagentSpawn({
      slot: 'A', id: 'sa-1', agent: 'test-agent',
    }))
    expect(state.slotActivity.A.subagents['sa-1'].status).toBe('running')
    expect(state.slotActivity.A.subagents['sa-1'].parentStopped).toBeUndefined()

    state = reducer(state, sseParentStopped({ slot: 'A' }))
    expect(state.slotActivity.A.subagents['sa-1'].parentStopped).toBe(true)
  })

  it('does not mark terminal (done) subagents', () => {
    let state = reducer(initial, sseSubagentSpawn({
      slot: 'A', id: 'sa-1', agent: 'test-agent',
    }))
    // Mark as done via spread (Immer freezes output)
    const doneSub = { ...state.slotActivity.A.subagents['sa-1'], status: 'done' as const }
    state = {
      ...state,
      slotActivity: {
        ...state.slotActivity,
        A: {
          ...state.slotActivity.A,
          subagents: { ...state.slotActivity.A.subagents, 'sa-1': doneSub },
        },
      },
    }

    state = reducer(state, sseParentStopped({ slot: 'A' }))
    expect(state.slotActivity.A.subagents['sa-1'].parentStopped).toBeUndefined()
  })

  it('handles non-existent slot gracefully', () => {
    const state = reducer(initial, sseParentStopped({ slot: 'nonexistent' }))
    expect(state).toBeDefined()
  })

  it('rejects prototype-pollution keys', () => {
    const state = reducer(initial, sseParentStopped({ slot: '__proto__' }))
    expect(state).toBeDefined()
  })

  it('mirrors the flag in the flat subagents map when slot is active', () => {
    // Set activeSlot to A so sseSubagentSpawn writes to state.subagents
    let state = reducer(initial, setActiveSlot('A'))
    state = reducer(state, sseSubagentSpawn({
      slot: 'A', id: 'sa-1', agent: 'test-agent',
    }))
    expect(state.subagents['sa-1']).toBeDefined()
    expect(state.subagents['sa-1'].status).toBe('running')

    state = reducer(state, sseParentStopped({ slot: 'A' }))
    expect(state.subagents['sa-1'].parentStopped).toBe(true)
  })
})
