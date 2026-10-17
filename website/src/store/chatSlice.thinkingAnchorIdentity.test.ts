/**
 * A reasoning block re-attaches to its turn by the IDENTITY of the row it
 * precedes, not by that row's text.
 *
 * Reasoning is client-only, so every refresh rebuilds the transcript from
 * server history and re-seats each block before the row that followed it.
 * That row is a tool call (server-minted `tool_call_id`) or an answer (server-
 * minted `meta.mid`), and both ids are stable for the row's lifetime. These
 * tests pin the cases where the row's identity holds but its CONTENT or its
 * position in the client list does not:
 *
 *  - switching an answer variant rewrites the row's text and `ts` under the
 *    same `mid`;
 *  - a racing refresh (a WS reconnect) snapshots the server before the tool
 *    row lands, so the block survives at the tail with its anchor row gone
 *    from the list, and the next scan from there names the wrong row or none.
 *
 * The block records its anchor's id on itself (`meta.thinkingAnchor`) the
 * first time it is seen ahead of an identified row, and both re-attachment
 * layers look that id up. Text matching remains only for an anchor that has
 * no id (a row restored from a transcript written before row ids existed, or
 * a live row the server has not confirmed yet), and those cases are pinned
 * here too so the fallback stays reachable.
 *
 * Driven through the real reducer where a reducer owns the path, so the
 * guarantee cannot be met by a helper the reducers do not call.
 */
import { describe, it, expect } from 'vitest'
import reducer, { sseThinkingChunk, sseChatMessage, refreshSlot } from './chatSlice'
import { mergePreservedThinking, reinsertThinkingOrphans, type ParkedThinking } from './chat/thinking'
import type { ChatMessage } from '../types'

type State = ReturnType<typeof reducer>

const SLOT = 'default'

const USER = { role: 'user', content: 'do the thing', cls: 'msg msg-u', ts: '2026-08-01T10:00:00Z', meta: { mid: 'u1' } }

/** A store with one committed user turn, ready to receive reasoning. */
function withOpenTurn(): State {
  let s = reducer(undefined, { type: '@@INIT' })
  s = { ...s, activeSlot: SLOT }
  s = reducer(s, sseChatMessage({ slot: SLOT, ...USER }))
  return s
}

/** Drive a slot refresh with the given server page. */
function refreshWith(s: State, serverMessages: Array<Record<string, unknown>>, opts: { running?: boolean; hasMore?: boolean } = {}): State {
  return reducer(s, {
    type: refreshSlot.fulfilled.type,
    payload: { key: SLOT, messages: serverMessages, running: opts.running ?? false, hasMore: opts.hasMore ?? false, queue: [], nextBefore: 0 },
    meta: { arg: SLOT },
  })
}

const thinkingRows = (s: State) => s.messages.filter(m => m.role === 'thinking')
const shape = (s: State) => s.messages.map(m => (m.role === 'thinking' ? `thinking:${m.content}` : `${m.role}:${m.content}`))

describe('reasoning keeps its turn when the answer text changes under the same row id', () => {
  it('survives switching the answer variant', () => {
    let s = withOpenTurn()
    s = reducer(s, sseThinkingChunk({ slot: SLOT, content: 'reasoning for the retry' }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'second answer' }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'assistant', content: 'second answer', cls: 'msg msg-a', ts: '2026-08-01T10:00:05Z', meta: { mid: 'a1' } }))
    // The chat_done refresh: the server holds the regenerated answer, with both
    // variants recorded on the row.
    s = refreshWith(s, [
      USER,
      { role: 'assistant', content: 'second answer', cls: 'msg msg-a', ts: '2026-08-01T10:00:05Z', meta: { mid: 'a1' } },
    ])
    expect(shape(s)).toEqual(['user:do the thing', 'thinking:reasoning for the retry', 'assistant:second answer'])

    // The user flips to the first variant. The backend adopts that variant's
    // text AND its `ts` on the SAME row, then broadcasts a switch that the
    // client answers with this refresh.
    s = refreshWith(s, [
      USER,
      { role: 'assistant', content: 'first answer', cls: 'msg msg-a', ts: '2026-08-01T10:00:02Z', meta: { mid: 'a1' } },
    ])
    expect(shape(s)).toEqual(['user:do the thing', 'thinking:reasoning for the retry', 'assistant:first answer'])
  })

  it('re-seats a parked block whose answer row changed text under the same id', () => {
    const block = { role: 'thinking', content: 'parked reasoning', cls: '', meta: { clientTs: 'k1' } } as ChatMessage
    const parked: Array<ParkedThinking<ChatMessage>> = [
      { msg: block, anchor: { text: 'second answer', ts: '2026-08-01T10:00:05Z', mid: 'a1' }, occ: 0, occTotal: 1 },
    ]
    const list = [
      { ...USER },
      { role: 'assistant', content: 'first answer', cls: 'msg msg-a', ts: '2026-08-01T10:00:02Z', meta: { mid: 'a1' } },
    ] as ChatMessage[]
    // An incomplete window is exactly when text cannot be trusted; the id can.
    const { list: out, remaining } = reinsertThinkingOrphans(list, parked, false)
    expect(out.map(m => m.role)).toEqual(['user', 'thinking', 'assistant'])
    expect(remaining).toHaveLength(0)
  })
})

describe('reasoning keeps its anchor across a racing refresh', () => {
  /** Reasoning, then a tool call, then a refresh whose snapshot predates the
   *  tool row: the block survives at the tail with no row after it. */
  function racedBeforeTool(): State {
    let s = withOpenTurn()
    s = reducer(s, sseThinkingChunk({ slot: SLOT, content: 'look first' }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'tool', content: '🔧 bash', cls: 'msg msg-t', ts: '2026-08-01T10:00:01Z', meta: { tool_call_id: 'tc-1' } }))
    s = refreshWith(s, [USER], { running: true })
    expect(shape(s)).toEqual(['user:do the thing', 'thinking:look first'])
    return s
  }

  const TOOL = { role: 'tool', content: '🔧 bash', cls: 'msg msg-t', ts: '2026-08-01T10:00:01Z', meta: { tool_call_id: 'tc-1' } }

  it('re-seats the block above its tool call, not above the answer that streamed later', () => {
    let s = racedBeforeTool()
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'all done' }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'assistant', content: 'all done', cls: 'msg msg-a', ts: '2026-08-01T10:00:03Z', meta: { mid: 'a1' } }))
    s = refreshWith(s, [
      USER,
      TOOL,
      { role: 'assistant', content: 'all done', cls: 'msg msg-a', ts: '2026-08-01T10:00:03Z', meta: { mid: 'a1' } },
    ])
    expect(shape(s)).toEqual(['user:do the thing', 'thinking:look first', 'tool:🔧 bash', 'assistant:all done'])
  })

  it('keeps the block in its turn once a later turn is covered', () => {
    let s = racedBeforeTool()
    // The turn ends on the tool call: nothing streams after the race.
    s = refreshWith(s, [USER, TOOL])
    expect(shape(s)).toEqual(['user:do the thing', 'thinking:look first', 'tool:🔧 bash'])

    // A second turn, then its refresh. Without the recorded anchor the block's
    // forward scan meets the second turn's user row, reads the first turn as
    // over, and drops the reasoning.
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'user', content: 'and then?', cls: 'msg msg-u', ts: '2026-08-01T10:01:00Z', meta: { mid: 'u2' } }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'next answer' }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'assistant', content: 'next answer', cls: 'msg msg-a', ts: '2026-08-01T10:01:02Z', meta: { mid: 'a2' } }))
    s = refreshWith(s, [
      USER,
      TOOL,
      { role: 'user', content: 'and then?', cls: 'msg msg-u', ts: '2026-08-01T10:01:00Z', meta: { mid: 'u2' } },
      { role: 'assistant', content: 'next answer', cls: 'msg msg-a', ts: '2026-08-01T10:01:02Z', meta: { mid: 'a2' } },
    ])
    expect(shape(s)).toEqual([
      'user:do the thing', 'thinking:look first', 'tool:🔧 bash',
      'user:and then?', 'assistant:next answer',
    ])
  })

  it('records the anchor on the block itself', () => {
    const s = racedBeforeTool()
    expect(thinkingRows(s)[0].meta?.thinkingAnchor).toBe('tool:tc-1')
  })
})

describe('identical text never decides which turn a block belongs to', () => {
  it('two turns with identical reasoning and identical answers each keep their own block on a bounded page', () => {
    let s = withOpenTurn()
    s = reducer(s, sseThinkingChunk({ slot: SLOT, content: 'same thought' }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'Done.' }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'assistant', content: 'Done.', cls: 'msg msg-a', ts: '2026-08-01T10:00:02Z', meta: { mid: 'a1' } }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'user', content: 'again', cls: 'msg msg-u', ts: '2026-08-01T10:01:00Z', meta: { mid: 'u2' } }))
    s = reducer(s, sseThinkingChunk({ slot: SLOT, content: 'same thought' }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'Done.' }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'assistant', content: 'Done.', cls: 'msg msg-a', ts: '2026-08-01T10:01:02Z', meta: { mid: 'a2' } }))
    const second = { role: 'user', content: 'again', cls: 'msg msg-u', ts: '2026-08-01T10:01:00Z', meta: { mid: 'u2' } }
    const a1 = { role: 'assistant', content: 'Done.', cls: 'msg msg-a', ts: '2026-08-01T10:00:02Z', meta: { mid: 'a1' } }
    const a2 = { role: 'assistant', content: 'Done.', cls: 'msg msg-a', ts: '2026-08-01T10:01:02Z', meta: { mid: 'a2' } }
    s = refreshWith(s, [USER, a1, second, a2])
    const ids = thinkingRows(s).map(m => m.meta?.thinkingAnchor)
    expect(ids).toEqual(['mid:a1', 'mid:a2'])

    // A bounded page that holds only the SECOND turn: its block seats on its own
    // answer, and the first turn's block is not seated on that identical text.
    s = refreshWith(s, [second, a2], { hasMore: true })
    const seated = s.messages.filter(m => m.role === 'thinking' || m.role === 'assistant')
    expect(seated.map(m => (m.role === 'thinking' ? `thinking:${String(m.meta?.thinkingAnchor)}` : `assistant:${String(m.meta?.mid)}`)))
      .toEqual(['thinking:mid:a1', 'assistant:a1', 'thinking:mid:a2', 'assistant:a2'])
  })
})

describe('anchors with no id still re-attach by text', () => {
  it('seats a block on a legacy answer row that carries no row id', () => {
    const existing = [
      { role: 'user', content: 'q', cls: '', ts: '2026-08-01T10:00:00Z' },
      { role: 'thinking', content: 'old reasoning', cls: '', meta: { clientTs: 'k1' } },
      { role: 'assistant', content: 'legacy answer', cls: '', ts: '2026-08-01T10:00:02Z' },
    ] as ChatMessage[]
    const incoming = [
      { role: 'user', content: 'q', cls: '', ts: '2026-08-01T10:00:00Z' },
      { role: 'assistant', content: 'legacy answer', cls: '', ts: '2026-08-01T10:00:02Z' },
    ] as ChatMessage[]
    const out = mergePreservedThinking(existing, incoming)
    expect(out.map(m => m.role)).toEqual(['user', 'thinking', 'assistant'])
    // Nothing to record: the row has no id, so the block stays text-anchored.
    expect(out[1].meta?.thinkingAnchor).toBeUndefined()
  })

  it('seats a record parked without an id on a complete window', () => {
    const block = { role: 'thinking', content: 'parked', cls: '' } as ChatMessage
    const list = [
      { role: 'user', content: 'q', cls: '' },
      { role: 'assistant', content: 'only answer', cls: '' },
    ] as ChatMessage[]
    const { list: out, remaining } = reinsertThinkingOrphans(list, [{ msg: block, anchor: { text: 'only answer' } }], true)
    expect(out.map(m => m.role)).toEqual(['user', 'thinking', 'assistant'])
    expect(remaining).toHaveLength(0)
  })

  it('a recorded id whose row is gone parks the block instead of stranding it at the tail', () => {
    const existing = [
      { role: 'user', content: 'q1', cls: '', ts: '2026-08-01T10:00:00Z', meta: { mid: 'u1' } },
      { role: 'assistant', content: 'a', cls: '', ts: '2026-08-01T10:00:02Z', meta: { mid: 'a1' } },
      { role: 'thinking', content: 'orphan', cls: '', meta: { clientTs: 'k1', thinkingAnchor: 'tool:gone' } },
      { role: 'user', content: 'q2', cls: '', ts: '2026-08-01T10:01:00Z', meta: { mid: 'u2' } },
      { role: 'assistant', content: 'b', cls: '', ts: '2026-08-01T10:01:02Z', meta: { mid: 'a2' } },
    ] as ChatMessage[]
    const incoming = existing.filter(m => m.role !== 'thinking')
    const sink: Array<ParkedThinking<ChatMessage>> = []
    const out = mergePreservedThinking(existing, incoming, incoming, true, sink)
    expect(out.some(m => m.role === 'thinking')).toBe(false)
    expect(sink.map(p => p.anchor)).toEqual([{ tool: 'gone' }])
  })
})
