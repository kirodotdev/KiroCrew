/**
 * Subagent panel identity and lazy-upsert tests (#759).
 *
 * Defect 2: an incremental update for an unknown agent must create a card
 * (not silently discard the frame).
 *
 * Defect 3: two updates for the same identity must update one card, not
 * create duplicates.
 */
import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, {
  setActiveSlot,
  sseSubagentSpawn,
  sseSubagentChunk,
  sseSubagentTool,
  sseSubagentDone,
  sseSubagentStalled,
  sseSubagentRetrying,
  sseSubagentBatchUpdate,
  sseSubagentBatchChunks,
} from './chatSlice'
import dashboardReducer from './dashboardSlice'
import notificationsReducer from './notificationsSlice'

function makeStore() {
  return configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
}

describe('Subagent panel: lazy upsert on incremental updates (#759 defect 2)', () => {
  it('sseSubagentChunk creates a card when spawn frame was missed', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('s1'))
    // No spawn event - go straight to chunk
    store.dispatch(sseSubagentChunk({ slot: 's1', id: 'abc12345', text: 'hello' }))

    const subs = store.getState().chat.subagents
    expect(subs['abc12345']).toBeDefined()
    expect(subs['abc12345'].id).toBe('abc12345')
    expect(subs['abc12345'].status).toBe('running')
    expect(subs['abc12345'].streaming).toBe('hello')
  })

  it('sseSubagentTool creates a card when spawn frame was missed', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('s1'))
    store.dispatch(sseSubagentTool({ slot: 's1', id: 'tool1234', tool: 'read_file', tool_count: 1 }))

    const subs = store.getState().chat.subagents
    expect(subs['tool1234']).toBeDefined()
    expect(subs['tool1234'].lastTool).toBe('read_file')
    expect(subs['tool1234'].status).toBe('tool')
  })

  it('sseSubagentStalled creates a card when spawn frame was missed', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('s1'))
    store.dispatch(sseSubagentStalled({ slot: 's1', id: 'stall123', stalled: true }))

    const subs = store.getState().chat.subagents
    expect(subs['stall123']).toBeDefined()
    expect(subs['stall123'].stalled).toBe(true)
  })

  it('sseSubagentRetrying creates a card when spawn frame was missed', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('s1'))
    store.dispatch(sseSubagentRetrying({ slot: 's1', id: 'retry123', attempt: 1 }))

    const subs = store.getState().chat.subagents
    expect(subs['retry123']).toBeDefined()
    expect(subs['retry123'].retrying).toBe(true)
  })

  it('sseSubagentBatchUpdate creates cards for unknown ids', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('s1'))
    store.dispatch(sseSubagentBatchUpdate({
      updates: [
        { slot: 's1', id: 'batch001', tool: 'write', tool_count: 3 },
        { slot: 's1', id: 'batch002', stalled: true },
      ],
    }))

    const subs = store.getState().chat.subagents
    expect(subs['batch001']).toBeDefined()
    expect(subs['batch001'].lastTool).toBe('write')
    expect(subs['batch002']).toBeDefined()
    expect(subs['batch002'].stalled).toBe(true)
  })

  it('sseSubagentBatchChunks creates a card for unknown id', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('s1'))
    store.dispatch(sseSubagentBatchChunks({
      chunks: [{ slot: 's1', id: 'bchunk01', text: 'streaming text' }],
    }))

    const subs = store.getState().chat.subagents
    expect(subs['bchunk01']).toBeDefined()
    expect(subs['bchunk01'].streaming).toBe('streaming text')
  })
})

describe('Subagent panel: identity deduplication (#759 defect 3)', () => {
  it('two chunk updates for the same id update ONE card, not two', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('s1'))
    store.dispatch(sseSubagentChunk({ slot: 's1', id: 'dedup123', text: 'first ' }))
    store.dispatch(sseSubagentChunk({ slot: 's1', id: 'dedup123', text: 'second' }))

    const subs = store.getState().chat.subagents
    const keys = Object.keys(subs).filter(k => k === 'dedup123')
    expect(keys).toHaveLength(1)
    expect(subs['dedup123'].streaming).toBe('first second')
  })

  it('a spawn frame after a lazy-created card fills in identity fields', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('s1'))
    // First: chunk arrives (creates placeholder)
    store.dispatch(sseSubagentChunk({ slot: 's1', id: 'fill1234', text: 'output' }))
    expect(store.getState().chat.subagents['fill1234'].task).toBe('')

    // Then: spawn event arrives (fills in identity)
    store.dispatch(sseSubagentSpawn({ slot: 's1', id: 'fill1234', task: 'search for X', agent: 'researcher' }))

    const card = store.getState().chat.subagents['fill1234']
    expect(card.task).toBe('search for X')
    expect(card.agent).toBe('researcher')
    expect(card.streaming).toBe('output')  // preserved from chunk
  })

  it('a done event after a lazy-created card correctly terminates it', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('s1'))
    store.dispatch(sseSubagentTool({ slot: 's1', id: 'done1234', tool: 'shell' }))
    store.dispatch(sseSubagentDone({ slot: 's1', id: 'done1234', elapsed: 42 }))

    const card = store.getState().chat.subagents['done1234']
    expect(card.status).toBe('done')
    expect(card.elapsed).toBe(42)
  })

  it('prototype-pollution ids are rejected even with lazy upsert', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('s1'))
    store.dispatch(sseSubagentChunk({ slot: 's1', id: '__proto__', text: 'evil' }))
    store.dispatch(sseSubagentTool({ slot: 's1', id: 'constructor', tool: 'x' }))
    store.dispatch(sseSubagentStalled({ slot: 's1', id: 'prototype', stalled: true }))

    const subs = store.getState().chat.subagents
    // Use Object.hasOwn - direct bracket access on __proto__ returns Object.prototype
    expect(Object.hasOwn(subs, '__proto__')).toBe(false)
    expect(Object.hasOwn(subs, 'constructor')).toBe(false)
    expect(Object.hasOwn(subs, 'prototype')).toBe(false)
  })
})
