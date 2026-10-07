/**
 * Subagent tool-call timeline (#13628): `lastTool` is overwritten by every
 * `subagent_tool` frame, so the card used to show one tool name for a whole
 * run. `toolCalls` keeps the recent calls in order. These pin:
 *  - each call is listed once, in arrival order;
 *  - a gated call's second frame (same `tool_count`) updates the entry rather
 *    than listing the call twice;
 *  - the coalesced batch frame feeds the same timeline;
 *  - the list is capped, while `toolCount` keeps the true total;
 *  - a reconnect snapshot keeps the timeline the card already had.
 */
import { describe, it, expect } from 'vitest'
import { createTestStore } from './helpers'
import { setActiveSlot, sseSubagentSpawn, sseSubagentTool, sseSubagentBatchUpdate, sseSubagentSnapshot } from '../store/chatSlice'
import { SUBAGENT_TOOL_CALLS_CAP } from '../store/chat/subagents'

const ID = 'tl0001'

function setup() {
  const store = createTestStore()
  const slot = 'chat-tool-timeline'
  store.dispatch(setActiveSlot(slot))
  store.dispatch(sseSubagentSpawn({ slot, id: ID, task: 't', agent: 'kirocrew' }))
  return { store, slot }
}
const sub = (store: ReturnType<typeof createTestStore>) => store.getState().chat.subagents[ID]
const tools = (store: ReturnType<typeof createTestStore>) => (sub(store).toolCalls ?? []).map(c => c.tool)

describe('subagent tool-call timeline', () => {
  it('lists every call in arrival order instead of only the last one', () => {
    const { store, slot } = setup()
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Running: ls', tool_count: 1 }))
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Reading src/a.ts', tool_count: 2 }))
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Running: npm test', tool_count: 3 }))
    expect(tools(store)).toEqual(['Running: ls', 'Reading src/a.ts', 'Running: npm test'])
    expect(sub(store).lastTool).toBe('Running: npm test')
  })

  it('a gated call sending a second frame with the same tool_count is listed once', () => {
    const { store, slot } = setup()
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Terminal', tool_count: 1 }))
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Running: git status', tool_count: 1 }))
    expect(tools(store)).toEqual(['Running: git status'])
  })

  it('frames without tool_count are each listed', () => {
    const { store, slot } = setup()
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'a' }))
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'b' }))
    expect(tools(store)).toEqual(['a', 'b'])
  })

  it('the coalesced batch frame feeds the same timeline', () => {
    const { store, slot } = setup()
    store.dispatch(sseSubagentBatchUpdate({ updates: [{ id: ID, slot, tool: 'x', tool_count: 1 }] }))
    // Coalescing dropped calls 2..4; only the latest reached the client.
    store.dispatch(sseSubagentBatchUpdate({ updates: [{ id: ID, slot, tool: 'y', tool_count: 5 }] }))
    expect(tools(store)).toEqual(['x', 'y'])
    expect(sub(store).toolCount).toBe(5)
  })

  it('caps the list while toolCount keeps the true total', () => {
    const { store, slot } = setup()
    const n = SUBAGENT_TOOL_CALLS_CAP + 7
    for (let i = 1; i <= n; i++) store.dispatch(sseSubagentTool({ slot, id: ID, tool: `t${i}`, tool_count: i }))
    const list = tools(store)
    expect(list).toHaveLength(SUBAGENT_TOOL_CALLS_CAP)
    expect(list[0]).toBe('t8')
    expect(list[list.length - 1]).toBe(`t${n}`)
    expect(sub(store).toolCount).toBe(n)
  })

  it('a reconnect snapshot keeps the timeline the card already had', () => {
    const { store, slot } = setup()
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'a', tool_count: 1 }))
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'b', tool_count: 2 }))
    store.dispatch(sseSubagentSnapshot({ id: ID, slot, task: 't', agent: 'kirocrew', streaming: '', last_tool: 'b', started: 1, tool_count: 2 }))
    expect(tools(store)).toEqual(['a', 'b'])
  })
})
