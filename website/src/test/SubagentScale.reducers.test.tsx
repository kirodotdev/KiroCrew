/**
 * Sub-agent scale reducers (PR-4/PR-5, 60-100 concurrent agents):
 *  - sseSubagentBatchUpdate applies one coalesced ~1s frame of per-agent
 *    deltas (tool/stalled/retrying), replacing per-event frames at scale.
 *  - sseSubagentBatchChunks appends concatenated streaming text per agent
 *    with the same truncation semantics as per-event chunks.
 *  - selectSubagent drives the 1-click chip→transcript flow.
 *  - clearTerminalSubagents backs the "Dismiss done" batch control (drops
 *    terminal cards only; running agents untouched).
 */
import { describe, it, expect } from 'vitest'
import { createTestStore } from './helpers'
import { sseSubagentSpawn, sseSubagentDone, sseSubagentBatchUpdate, sseSubagentBatchChunks, selectSubagent, clearTerminalSubagents } from '../store/chatSlice'

function spawnMany(store: ReturnType<typeof createTestStore>, n: number) {
  const SLOT = store.getState().chat.activeSlot
  for (let i = 0; i < n; i++) {
    store.dispatch(sseSubagentSpawn({ slot: SLOT, id: `ag${i}`, task: `task ${i}`, agent: 'kirocrew' }))
  }
  return SLOT
}

describe('subagent scale reducers', () => {
  it('sseSubagentBatchUpdate applies merged deltas per agent in one action', () => {
    const store = createTestStore()
    const SLOT = spawnMany(store, 3)
    store.dispatch(sseSubagentBatchUpdate({ updates: [
      { id: 'ag0', slot: SLOT, tool: 'Read', tool_count: 4 },
      { id: 'ag1', slot: SLOT, stalled: true },
      { id: 'ag2', slot: SLOT, attempt: 1 },
    ] }))
    const subs = store.getState().chat.subagents
    expect(subs['ag0'].lastTool).toBe('Read')
    expect(subs['ag0'].toolCount).toBe(4)
    expect(subs['ag0'].status).toBe('tool')
    expect(subs['ag1'].stalled).toBe(true)
    expect(subs['ag2'].retrying).toBe(true)
  })

  it('sseSubagentBatchUpdate ignores unknown and unsafe ids', () => {
    const store = createTestStore()
    const SLOT = spawnMany(store, 1)
    store.dispatch(sseSubagentBatchUpdate({ updates: [
      { id: '__proto__', slot: SLOT, tool: 'evil' },
      { id: 'ghost', slot: SLOT, tool: 'Read' },
    ] }))
    expect(Object.keys(store.getState().chat.subagents)).toEqual(['ag0'])
    expect(({} as Record<string, unknown>).lastTool).toBeUndefined() // no prototype pollution
  })

  it('sseSubagentBatchChunks appends text with truncation semantics', () => {
    const store = createTestStore()
    const SLOT = spawnMany(store, 2)
    store.dispatch(sseSubagentBatchChunks({ chunks: [
      { id: 'ag0', slot: SLOT, text: 'hello ' },
      { id: 'ag1', slot: SLOT, text: 'other' },
    ] }))
    store.dispatch(sseSubagentBatchChunks({ chunks: [{ id: 'ag0', slot: SLOT, text: 'world' }] }))
    const subs = store.getState().chat.subagents
    expect(subs['ag0'].streaming).toBe('hello world')
    expect(subs['ag1'].streaming).toBe('other')
  })

  it('selectSubagent sets and clears the selection', () => {
    const store = createTestStore()
    store.dispatch(selectSubagent('ag5'))
    expect(store.getState().chat.selectedSubagentId).toBe('ag5')
    store.dispatch(selectSubagent(null))
    expect(store.getState().chat.selectedSubagentId).toBeNull()
  })

  it('clearTerminalSubagents drops only terminal cards', () => {
    const store = createTestStore()
    const SLOT = spawnMany(store, 3)
    store.dispatch(sseSubagentDone({ slot: SLOT, id: 'ag0', elapsed: 5, outcome: 'completed' }))
    store.dispatch(sseSubagentDone({ slot: SLOT, id: 'ag1', elapsed: 5, error: 'boom', outcome: 'failed' }))
    store.dispatch(clearTerminalSubagents({ slot: SLOT }))
    const subs = store.getState().chat.subagents
    expect(Object.keys(subs)).toEqual(['ag2']) // running agent survives
  })
})
