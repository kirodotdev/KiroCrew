/**
 * Subagent live mini-transcript: `SubagentActivity.transcript` holds what the
 * agent did, in arrival order, from the `subagent_thinking` / `subagent_tool` /
 * `subagent_tool_result` frames and the text chunks. These pin:
 *  - a tool frame opens a row; its refinement (same `tool_call_id`) refines
 *    that row instead of listing the call twice;
 *  - a result lands on the row the call opened, output and status alike, and a
 *    result for a call this browser never saw open opens an untitled row the
 *    refinement that follows names (a result-first call);
 *  - every row carries a `seq` that keeps counting past the cap;
 *  - consecutive reasoning or answer chunks are one row; a tool call splits them;
 *  - reasoning and a tool frame reach an agent the store does not hold yet;
 *  - the row list is capped and the omitted count is kept;
 *  - a reconnect snapshot keeps the transcript the card already had.
 */
import { describe, it, expect } from 'vitest'
import { createTestStore } from './helpers'
import {
  setActiveSlot, sseSubagentSpawn, sseSubagentTool, sseSubagentToolResult,
  sseSubagentBatchChunks, sseSubagentBatchUpdate, sseSubagentSnapshot,
} from '../store/chatSlice'

// Mirrors SUBAGENT_TRANSCRIPT_CAP in the subagent reducer module (the chatSlice
// facade's export list is pinned, so the number is restated here).
const SUBAGENT_TRANSCRIPT_CAP = 200

const ID = 'native:child-1'

function setup() {
  const store = createTestStore()
  const slot = 'chat-transcript'
  store.dispatch(setActiveSlot(slot))
  store.dispatch(sseSubagentSpawn({ slot, id: ID, task: 'review the commit', agent: 'reviewer' }))
  return { store, slot }
}
const sub = (store: ReturnType<typeof createTestStore>) => store.getState().chat.subagents[ID]
const rows = (store: ReturnType<typeof createTestStore>) => sub(store).transcript ?? []

describe('subagent live transcript', () => {
  it('opens a row per tool call and refines the same call instead of listing it twice', () => {
    const { store, slot } = setup()
    // kiro-cli announces a native child's call twice: bare name, then the title + input.
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'shell', tool_call_id: 'tc-1', tool_count: 1 }))
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Running: git log -1', tool_call_id: 'tc-1', tool_count: 1, input: '{"command": "git log -1"}' }))
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Reading a.ts', tool_call_id: 'tc-2', tool_count: 2 }))

    expect(rows(store)).toEqual([
      { kind: 'tool', seq: 0, id: 'tc-1', tool: 'Running: git log -1', input: '{"command": "git log -1"}' },
      { kind: 'tool', seq: 1, id: 'tc-2', tool: 'Reading a.ts' },
    ])
    expect(sub(store).status).toBe('tool')
  })

  it('lands a result on the row its call opened', () => {
    const { store, slot } = setup()
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Running: git log -1', tool_call_id: 'tc-1' }))

    store.dispatch(sseSubagentToolResult({ slot, id: ID, tool_call_id: 'tc-1', output: 'commit abc' }))
    store.dispatch(sseSubagentToolResult({ slot, id: ID, tool_call_id: 'tc-1', status: 'completed' }))

    expect(rows(store)).toEqual([
      { kind: 'tool', seq: 0, id: 'tc-1', tool: 'Running: git log -1', output: 'commit abc', status: 'completed' },
    ])
  })

  it('a result for a call never seen opening starts an untitled row that its refinement then names', () => {
    // A native child's opening frame can predate the child's admission (it
    // starts working at once), so the call is first seen through its result:
    // kiro-cli's terminal update arrives as the result, then the refinement.
    const { store, slot } = setup()
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Reading a.ts', tool_call_id: 'tc-0', tool_count: 1 }))

    store.dispatch(sseSubagentToolResult({ slot, id: ID, tool_call_id: 'tc-1', output: 'ok' }))
    store.dispatch(sseSubagentToolResult({ slot, id: ID, tool_call_id: 'tc-1', status: 'completed' }))
    expect(rows(store)).toEqual([
      { kind: 'tool', seq: 0, id: 'tc-0', tool: 'Reading a.ts' },
      { kind: 'tool', seq: 1, id: 'tc-1', tool: '', output: 'ok', status: 'completed' },
    ])

    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Running: npm test', tool_call_id: 'tc-1', tool_count: 2, input: '{"command": "npm test"}' }))
    expect(rows(store)).toEqual([
      { kind: 'tool', seq: 0, id: 'tc-0', tool: 'Reading a.ts' },
      { kind: 'tool', seq: 1, id: 'tc-1', tool: 'Running: npm test', input: '{"command": "npm test"}', output: 'ok', status: 'completed' },
    ])
    expect(sub(store).toolCount).toBe(2)
  })

  it('a result for an unknown call still claims the id-less row a snapshot seeded, before opening its own', () => {
    const { store, slot } = setup()
    store.dispatch(sseSubagentSnapshot({ id: ID, slot, task: 'review the commit', agent: 'reviewer', streaming: '', last_tool: 'Running: ls', started: 1 }))

    store.dispatch(sseSubagentToolResult({ slot, id: ID, tool_call_id: 'tc-1', output: 'a.ts', status: 'completed' }))

    expect(rows(store)).toEqual([{ kind: 'tool', seq: 0, id: 'tc-1', tool: 'Running: ls', output: 'a.ts', status: 'completed' }])
  })

  it('keeps consecutive chunks of one kind as one row and splits them on a tool call', () => {
    const { store, slot } = setup()
    store.dispatch(sseSubagentBatchChunks({ chunks: [{ slot, id: ID, text: 'plan ', kind: 'thinking' }] }))
    store.dispatch(sseSubagentBatchChunks({ chunks: [{ slot, id: ID, text: 'the review', kind: 'thinking' }] }))
    store.dispatch(sseSubagentBatchChunks({ chunks: [{ slot, id: ID, text: 'Looks ' }, { slot, id: ID, text: 'good.' }] }))
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Reading a.ts', tool_call_id: 'tc-1' }))
    store.dispatch(sseSubagentBatchChunks({ chunks: [{ slot, id: ID, text: 'Done.' }] }))

    expect(rows(store)).toEqual([
      { kind: 'thinking', seq: 0, text: 'plan the review' },
      { kind: 'text', seq: 1, text: 'Looks good.' },
      { kind: 'tool', seq: 2, id: 'tc-1', tool: 'Reading a.ts' },
      { kind: 'text', seq: 3, text: 'Done.' },
    ])
    // The streamed answer still accumulates where the Output box reads it;
    // reasoning stays out of it.
    expect(sub(store).streaming).toBe('Looks good.Done.')
  })

  it('a reasoning frame reaches an agent the store does not hold yet', () => {
    const store = createTestStore()
    const slot = 'chat-transcript'
    store.dispatch(setActiveSlot(slot))

    store.dispatch(sseSubagentBatchChunks({ chunks: [{ slot, id: ID, text: 'first words', kind: 'thinking' }] }))

    expect(sub(store)?.status).toBe('running')
    expect(rows(store)).toEqual([{ kind: 'thinking', seq: 0, text: 'first words' }])
  })

  it('the coalesced batch frame feeds the same transcript', () => {
    const { store, slot } = setup()
    store.dispatch(sseSubagentBatchUpdate({ updates: [{ slot, id: ID, tool: 'Running: ls', tool_call_id: 'tc-9', tool_count: 1 }] }))

    expect(rows(store)).toEqual([{ kind: 'tool', seq: 0, id: 'tc-9', tool: 'Running: ls' }])
    expect(sub(store).toolCount).toBe(1)
  })

  it('the batch frame carries only what the coalescer forwards: native-only fields on it are not read', () => {
    // Native cards' frames bypass the coalescer, so `input` never rides a
    // batch update; a frame that carried it anyway (a wire the client does not
    // know) must not be read as one.
    const { store, slot } = setup()
    const stray = { slot, id: ID, tool: 'Running: ls', tool_call_id: 'tc-9', tool_count: 1, input: '{"command": "ls"}' }
    store.dispatch(sseSubagentBatchUpdate({ updates: [stray as unknown as Parameters<typeof sseSubagentBatchUpdate>[0]['updates'][number]] }))

    expect(rows(store)).toEqual([{ kind: 'tool', seq: 0, id: 'tc-9', tool: 'Running: ls' }])
  })

  it("lands the roster's id-less re-send of an announced call on that call's row", () => {
    // kiro-cli's roster frame repeats a child's current status as a bare title
    // (no tool_call_id, no count) after the child announced the same call by
    // id. That is the same step, not a second one: an id-less frame keeps the
    // title rule whatever the newest row carries.
    const { store, slot } = setup()
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Running: npm test', tool_call_id: 'tc-1', tool_count: 1 }))

    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Running: npm test' }))

    expect(rows(store)).toEqual([{ kind: 'tool', seq: 0, id: 'tc-1', tool: 'Running: npm test' }])
    // A DIFFERENT id-less status is still its own step.
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Waiting for approval' }))
    expect(rows(store).map(r => r.kind === 'tool' && r.tool)).toEqual(['Running: npm test', 'Waiting for approval'])
  })

  it('caps the rows and counts what fell off', () => {
    const { store, slot } = setup()
    for (let i = 0; i < SUBAGENT_TRANSCRIPT_CAP + 7; i++) {
      store.dispatch(sseSubagentTool({ slot, id: ID, tool: `call ${i}`, tool_call_id: `tc-${i}` }))
    }

    expect(rows(store)).toHaveLength(SUBAGENT_TRANSCRIPT_CAP)
    expect(rows(store)[0]).toMatchObject({ tool: 'call 7' })
    expect(sub(store).transcriptOmitted).toBe(7)
  })

  it('a row keeps its seq while the cap drops rows off the front, and no seq is reused', () => {
    // The row key the card renders with: a position would shift on every drop,
    // handing an expanded row's state to the row that slid into its place.
    const { store, slot } = setup()
    for (let i = 0; i < SUBAGENT_TRANSCRIPT_CAP; i++) {
      store.dispatch(sseSubagentTool({ slot, id: ID, tool: `call ${i}`, tool_call_id: `tc-${i}` }))
    }
    const before = rows(store).map(r => r.seq)
    expect(before).toEqual(Array.from({ length: SUBAGENT_TRANSCRIPT_CAP }, (_, i) => i))

    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'one more', tool_call_id: 'tc-more' }))

    const after = rows(store)
    expect(after[0]).toMatchObject({ tool: 'call 1', seq: 1 })
    expect(after[after.length - 1]).toMatchObject({ tool: 'one more', seq: SUBAGENT_TRANSCRIPT_CAP })
    expect(new Set(after.map(r => r.seq)).size).toBe(after.length)
  })

  it('a reconnect snapshot keeps the transcript the card already had', () => {
    const { store, slot } = setup()
    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Running: ls', tool_call_id: 'tc-1' }))

    store.dispatch(sseSubagentSnapshot({ id: ID, slot, task: 'review the commit', agent: 'reviewer', streaming: '', last_tool: 'Running: ls', started: 1 }))

    expect(rows(store)).toEqual([{ kind: 'tool', seq: 0, id: 'tc-1', tool: 'Running: ls' }])
  })

  it('rows a snapshot seeded and the live rows after them share one seq sequence', () => {
    const { store, slot } = setup()
    store.dispatch(sseSubagentSnapshot({ id: ID, slot, task: 'review the commit', agent: 'reviewer', streaming: 'so far', last_tool: 'Running: ls', started: 1 }))

    store.dispatch(sseSubagentTool({ slot, id: ID, tool: 'Reading a.ts', tool_call_id: 'tc-2' }))

    expect(rows(store).map(r => r.seq)).toEqual([0, 1, 2])
  })
})
