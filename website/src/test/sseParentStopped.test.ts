import { describe, it, expect } from 'vitest'
import reducer, { sseChatMessage, sseSubagentSpawn, sseSubagentSnapshot, setActiveSlot, setSlotRunning, startLocalTurn, syncSlotRunningFromServer } from '../store/chatSlice'

// The Stop card frame the gateway pushes through the chat stream, settled at *state*.
const stopCard = (slot: string, state = 'stopped') => sseChatMessage({
  slot, role: 'system', content: '', ts: '2026-11-07T00:00:00Z', kind: 'stop_event',
  meta: { kind: 'stop_event', id: 'stop-1', state },
} as Parameters<typeof sseChatMessage>[0])

// Get properly initialized state through the reducer
const initial = reducer(undefined, { type: '@@INIT' })

describe('parent-stopped mark from the Stop card', () => {
  it.each(['stopping', 'stop_declined_compacting'])('leaves running agents alone on a %s card', cardState => {
    let state = reducer(initial, sseSubagentSpawn({ slot: 'A', id: 'sa-1', agent: 'test-agent' }))
    state = reducer(state, stopCard('A', cardState))
    expect(state.slotActivity.A.subagents['sa-1'].parentStopped).toBeUndefined()
  })

  it.each(['force_stopped', 'stop_failed_reset'])('marks running agents on a %s card', cardState => {
    let state = reducer(initial, sseSubagentSpawn({ slot: 'A', id: 'sa-1', agent: 'test-agent' }))
    state = reducer(state, stopCard('A', cardState))
    expect(state.slotActivity.A.subagents['sa-1'].parentStopped).toBe(true)
  })

  it('marks active subagents as parentStopped', () => {
    let state = reducer(initial, sseSubagentSpawn({
      slot: 'A', id: 'sa-1', agent: 'test-agent',
    }))
    expect(state.slotActivity.A.subagents['sa-1'].status).toBe('running')
    expect(state.slotActivity.A.subagents['sa-1'].parentStopped).toBeUndefined()

    state = reducer(state, stopCard('A'))
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

    state = reducer(state, stopCard('A'))
    expect(state.slotActivity.A.subagents['sa-1'].parentStopped).toBeUndefined()
  })

  it('handles non-existent slot gracefully', () => {
    const state = reducer(initial, stopCard('nonexistent'))
    expect(state).toBeDefined()
  })

  it('rejects prototype-pollution keys', () => {
    const state = reducer(initial, stopCard('__proto__'))
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

    state = reducer(state, stopCard('A'))
    expect(state.subagents['sa-1'].parentStopped).toBe(true)
  })

  it('lifts the mark when a new parent turn starts on the slot', () => {
    let state = reducer(initial, setActiveSlot('A'))
    state = reducer(state, sseSubagentSpawn({ slot: 'A', id: 'sa-1', agent: 'test-agent' }))
    state = reducer(state, stopCard('A'))
    state = reducer(state, startLocalTurn('A'))
    expect(state.subagents['sa-1'].parentStopped).toBeUndefined()

    state = reducer(state, setSlotRunning(false))
    state = reducer(state, stopCard('A'))
    state = reducer(state, setSlotRunning(true))
    expect(state.subagents['sa-1'].parentStopped).toBeUndefined()
  })

  it('lifts the mark when an inject row starts the next parent turn', () => {
    let state = reducer(initial, setActiveSlot('A'))
    state = reducer(state, sseSubagentSpawn({ slot: 'A', id: 'sa-1', agent: 'test-agent' }))
    state = reducer(state, stopCard('A'))
    expect(state.subagents['sa-1'].parentStopped).toBe(true)
    state = reducer(state, sseChatMessage({
      slot: 'A', role: 'inject', content: '[Subagent completion event] sa-2 done', ts: '2026-11-07T00:01:00Z',
    } as Parameters<typeof sseChatMessage>[0]))
    expect(state.subagents['sa-1'].parentStopped).toBeUndefined()
  })

  it('lifts the mark when the slots sync sees a cron or Slack turn start', () => {
    let state = reducer(initial, setActiveSlot('A'))
    state = reducer(state, sseSubagentSpawn({ slot: 'A', id: 'sa-1', agent: 'test-agent' }))
    state = reducer(state, stopCard('A'))
    state = reducer(state, syncSlotRunningFromServer({ slot: 'A', running: true, stopping: false }))
    expect(state.subagents['sa-1'].parentStopped).toBeUndefined()
  })

  it('keeps the mark when a repeat running=true arrives while the Stop settles', () => {
    let state = reducer(initial, setActiveSlot('A'))
    state = reducer(state, setSlotRunning(true))
    state = reducer(state, sseSubagentSpawn({ slot: 'A', id: 'sa-1', agent: 'test-agent' }))
    state = reducer(state, stopCard('A'))
    state = reducer(state, setSlotRunning(true))
    expect(state.subagents['sa-1'].parentStopped).toBe(true)
  })

  it('keeps the mark across a reconnect snapshot of the same running agent', () => {
    let state = reducer(initial, sseSubagentSpawn({ slot: 'A', id: 'sa-1', agent: 'test-agent' }))
    state = reducer(state, stopCard('A'))
    state = reducer(state, sseSubagentSnapshot({
      slot: 'A', id: 'sa-1', task: 't', agent: 'test-agent', streaming: '', last_tool: '', started: 1, tool_count: 0,
    }))
    expect(state.slotActivity.A.subagents['sa-1'].parentStopped).toBe(true)
  })
})
