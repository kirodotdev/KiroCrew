/**
 * The card's tool-call timeline (#13628) lives in its mini-transcript: `lastTool`
 * is overwritten by every `subagent_tool` frame, so the card used to show one
 * tool name for a whole run. The transcript's tool rows keep the calls in order,
 * between the reasoning and text rows. These pin:
 *  - each call is one row, in arrival order;
 *  - a gated call's second frame (same `tool_count`, or the same id) refines the
 *    row rather than listing the call twice;
 *  - the coalesced batch frame feeds the same rows;
 *  - the transcript is capped, while `toolCount` keeps the true total;
 *  - a reconnect snapshot keeps the rows the card already had, and seeds a
 *    rebuilt card from the snapshot's feed and latest tool.
 */
import { describe, it, expect } from 'vitest'
import { createTestStore } from './helpers'
import { setActiveSlot, sseSubagentSpawn, sseSubagentTool, sseSubagentToolResult, sseSubagentBatchUpdate, sseSubagentSnapshot } from '../store/chatSlice'

// Mirrors SUBAGENT_TRANSCRIPT_CAP in the subagent reducer module. Tests outside
// src/store may import the store only through the chatSlice facade, whose
// export list is pinned, so the number is restated here.
const SUBAGENT_TRANSCRIPT_CAP = 200

const ID = 'tl0001'

function setup() {
  const store = createTestStore()
  const slot = 'chat-tool-timeline'
  store.dispatch(setActiveSlot(slot))
  store.dispatch(sseSubagentSpawn({ slot, id: ID, task: 't', agent: 'kirocrew' }))
  return { store, slot }
}
const sub = (store: ReturnType<typeof createTestStore>) => store.getState().chat.subagents[ID]
const rows = (store: ReturnType<typeof createTestStore>) => sub(store).transcript ?? []
const tools = (store: ReturnType<typeof createTestStore>) => rows(store).flatMap(r => (r.kind === 'tool' ? [r.tool] : []))

describe('subagent tool rows in the transcript', () => {
  it('lists every call in arrival order instead of only the last one', () => {
    const { store, slot } = setup()
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Running: ls', tool_count: 1 }))
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Reading src/a.ts', tool_count: 2 }))
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Running: npm test', tool_count: 3 }))
    expect(tools(store)).toEqual(['Running: ls', 'Reading src/a.ts', 'Running: npm test'])
    expect(sub(store).lastTool).toBe('Running: npm test')
  })

  it('a gated call sending a second frame with the same tool_count is one row', () => {
    const { store, slot } = setup()
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Terminal', tool_count: 1 }))
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Running: git status', tool_count: 1 }))
    expect(tools(store)).toEqual(['Running: git status'])
  })

  it('frames without tool_count but with new titles are each a row', () => {
    const { store, slot } = setup()
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'a' }))
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'b' }))
    expect(tools(store)).toEqual(['a', 'b'])
  })

  it('a native card re-sending the same status without a count is one row', () => {
    // `_native_subagent_sync` re-broadcasts an unchanged status on every
    // list_update, with no tool_count.
    const { store, slot } = setup()
    for (let i = 0; i < 5; i++) store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Reading files' }))
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Running tests' }))
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Running tests' }))
    expect(tools(store)).toEqual(['Reading files', 'Running tests'])
  })

  it('a counted frame with a repeated title is a new call', () => {
    const { store, slot } = setup()
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Running: npm test', tool_count: 1 }))
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Running: npm test', tool_count: 2 }))
    expect(tools(store)).toEqual(['Running: npm test', 'Running: npm test'])
  })

  it('the two frames of one gated call share a tool_call_id and are one row', () => {
    const { store, slot } = setup()
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Terminal', tool_count: 1, tool_call_id: 'c1' }))
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Running: git status', tool_count: 1, tool_call_id: 'c1' }))
    expect(tools(store)).toEqual(['Running: git status'])
  })

  it('a child permission request with an unmoved count but a new id is a new call', () => {
    // run.py counts a backend child's request only at approval, so its frame
    // repeats the parent's tool_count; it must not overwrite the parent's call.
    const { store, slot } = setup()
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'spawn child', tool_count: 1, tool_call_id: 'p1' }))
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'child: Running: rm x', tool_count: 1, tool_call_id: 'k1' }))
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'child: Running: rm y', tool_count: 1, tool_call_id: 'k2' }))
    expect(tools(store)).toEqual(['spawn child', 'child: Running: rm x', 'child: Running: rm y'])
  })

  it('the coalesced batch frame feeds the same rows', () => {
    const { store, slot } = setup()
    store.dispatch(sseSubagentBatchUpdate({ updates: [{ id: ID, slot, tool: 'x', tool_count: 1 }] }))
    // Coalescing dropped calls 2..4; only the latest reached the client.
    store.dispatch(sseSubagentBatchUpdate({ updates: [{ id: ID, slot, tool: 'y', tool_count: 5 }] }))
    expect(tools(store)).toEqual(['x', 'y'])
    expect(sub(store).toolCount).toBe(5)
  })

  it('caps the transcript while toolCount keeps the true total', () => {
    const { store, slot } = setup()
    const n = SUBAGENT_TRANSCRIPT_CAP + 7
    for (let i = 1; i <= n; i++) store.dispatch(sseSubagentTool({ slot, id: ID, tool: `t${i}`, tool_count: i }))
    const list = tools(store)
    expect(list).toHaveLength(SUBAGENT_TRANSCRIPT_CAP)
    expect(list[0]).toBe('t8')
    expect(list[list.length - 1]).toBe(`t${n}`)
    expect(sub(store).transcriptOmitted).toBe(7)
    expect(sub(store).toolCount).toBe(n)
  })

  it('a reconnect snapshot keeps the rows the card already had', () => {
    const { store, slot } = setup()
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'a', tool_count: 1 }))
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'b', tool_count: 2 }))
    store.dispatch(sseSubagentSnapshot({ id: ID, slot, task: 't', agent: 'kirocrew', streaming: '', last_tool: 'b', started: 1, tool_count: 2 }))
    expect(tools(store)).toEqual(['a', 'b'])
  })

  it('a rebuilt card starts from the snapshot: the feed so far, then the call in flight', () => {
    // After a reload the running card is gone and the snapshot is all this
    // browser has; what the agent did before the reload must stay in view.
    const store = createTestStore()
    const slot = 'chat-tool-timeline'
    store.dispatch(setActiveSlot(slot))
    store.dispatch(sseSubagentSnapshot({ id: ID, slot, task: 't', agent: 'kirocrew', streaming: '→ Reading a.ts\nconst a = 1\n', last_tool: 'Running: npm test', started: 1, tool_count: 2 }))
    expect(rows(store)).toEqual([
      { kind: 'text', seq: 0, text: '→ Reading a.ts\nconst a = 1\n' },
      { kind: 'tool', seq: 1, tool: 'Running: npm test' },
    ])
  })

  it('a snapshot with neither feed nor tool seeds no transcript', () => {
    const store = createTestStore()
    const slot = 'chat-tool-timeline'
    store.dispatch(setActiveSlot(slot))
    store.dispatch(sseSubagentSnapshot({ id: ID, slot, task: 't', agent: 'kirocrew', streaming: '', last_tool: '', started: 1 }))
    expect(sub(store).transcript).toBeUndefined()
  })

  it('an id-bearing frame after a replay-seeded row is its own call', () => {
    // The replay seeds one row with no id; a child's request then arrives with
    // an id and the same tool_count, so the count alone would merge it.
    const store = createTestStore()
    const slot = 'chat-tool-timeline'
    store.dispatch(setActiveSlot(slot))
    store.dispatch(sseSubagentSnapshot({ id: ID, slot, task: 't', agent: 'kirocrew', streaming: '', last_tool: 'spawn child', started: 1, tool_count: 1 }))
    expect(tools(store)).toEqual(['spawn child'])
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'child: Running: rm x', tool_count: 1, tool_call_id: 'k1' }))
    expect(tools(store)).toEqual(['spawn child', 'child: Running: rm x'])
  })

  it('the result of the call a snapshot seeded lands on that row', () => {
    // The call opened before the reload, so no frame of this browser carries
    // its id; the seeded row is the call in flight and still unresolved.
    const store = createTestStore()
    const slot = 'chat-tool-timeline'
    store.dispatch(setActiveSlot(slot))
    store.dispatch(sseSubagentSnapshot({ id: ID, slot, task: 't', agent: 'kirocrew', streaming: '', last_tool: 'Running: npm test', started: 1, tool_count: 1 }))
    store.dispatch(sseSubagentToolResult({ slot, id: ID, tool_call_id: 'k9', output: '12 passed', status: 'completed' }))
    expect(rows(store)).toEqual([{ kind: 'tool', seq: 0, tool: 'Running: npm test', id: 'k9', output: '12 passed', status: 'completed' }])
    // A later result for the same id lands by id; one for another unseen call
    // finds the seeded row claimed and opens its own untitled row.
    store.dispatch(sseSubagentToolResult({ slot, id: ID, tool_call_id: 'k10', output: 'kept', status: 'completed' }))
    expect(tools(store)).toEqual(['Running: npm test', ''])
    expect(rows(store)[0]).toMatchObject({ output: '12 passed' })
    expect(rows(store)[1]).toMatchObject({ id: 'k10', output: 'kept', status: 'completed' })
  })
})
