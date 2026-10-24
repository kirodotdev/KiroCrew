/**
 * Tests for the sseParentStopped reducer — the frontend half of the
 * dashboard Stop → subagent spinner fix (#18625).
 */
import { describe, it, expect } from 'vitest'
import reducer, { sseParentStopped } from '../store/chatSlice'
import type { SubagentActivity } from '../types'

const initial = reducer(undefined, { type: '@@INIT' })

function mkAgent(id: string, status: SubagentActivity['status'] = 'running'): SubagentActivity {
  return {
    id, task: 'read something', agent: 'kirocrew',
    status, streaming: '', lastTool: '', startedAt: Date.now(), elapsed: 0,
  }
}

describe('sseParentStopped reducer', () => {
  it('marks active subagents as parentStopped on the active slot', () => {
    const state = {
      ...initial,
      activeSlot: 'slot-1',
      subagents: { a1: mkAgent('a1', 'running') },
    }
    const next = reducer(state, sseParentStopped({ slot: 'slot-1' }))
    expect(next.subagents['a1'].parentStopped).toBe(true)
  })

  it('does not mark terminal subagents', () => {
    const state = {
      ...initial,
      activeSlot: 'slot-1',
      subagents: { a1: mkAgent('a1', 'done') },
    }
    const next = reducer(state, sseParentStopped({ slot: 'slot-1' }))
    expect(next.subagents['a1'].parentStopped).toBeUndefined()
  })

  it('marks active subagents on a non-active slot', () => {
    const state = {
      ...initial,
      activeSlot: 'slot-other',
      slotActivity: {
        'slot-1': {
          toolLog: [],
          subagents: { a1: mkAgent('a1', 'tool') },
        },
      },
    }
    const next = reducer(state, sseParentStopped({ slot: 'slot-1' }))
    expect(next.slotActivity['slot-1'].subagents['a1'].parentStopped).toBe(true)
  })

  it('does nothing for an unknown slot', () => {
    const state = {
      ...initial,
      activeSlot: 'slot-1',
      subagents: { a1: mkAgent('a1', 'running') },
    }
    const next = reducer(state, sseParentStopped({ slot: 'slot-999' }))
    expect(next.subagents['a1'].parentStopped).toBeUndefined()
  })

  it('refuses an unsafe slot key', () => {
    const state = {
      ...initial,
      activeSlot: 'slot-1',
      subagents: { a1: mkAgent('a1', 'running') },
    }
    const next = reducer(state, sseParentStopped({ slot: '__proto__' }))
    expect(next.subagents['a1'].parentStopped).toBeUndefined()
  })
})
