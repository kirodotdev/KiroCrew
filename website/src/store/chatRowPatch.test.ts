import { describe, expect, it } from 'vitest'

import { applyRowPatch, logRowPatch, rowPatchesSince, withReplayedPatches } from './chat/rowPatch'
import { ROW_PATCH_LOG_CAP, type ChatState } from './chat/state'
import type { ChatMessage } from '../types'

const chat = () => ({ rowPatchLog: [], rowPatchSeq: 0 }) as unknown as ChatState

describe('rowPatch', () => {
  it('returns the patches to one slot numbered past a mark, and none when nothing moved', () => {
    const state = chat()
    logRowPatch(state, { slot: 'a', mid: 'm-1', content: 'one' })
    const mark = state.rowPatchSeq
    expect(rowPatchesSince(state, 'a', mark)).toEqual([])
    logRowPatch(state, { slot: 'b', mid: 'm-2', content: 'other' })
    logRowPatch(state, { slot: 'a', mid: 'm-1', content: 'two' })
    expect(rowPatchesSince(state, 'a', mark)?.map(p => p.content)).toEqual(['two'])
  })

  it('answers null once more patches landed than the log keeps', () => {
    const state = chat()
    for (let i = 0; i < ROW_PATCH_LOG_CAP + 5; i++) logRowPatch(state, { slot: 'a', mid: 'm-1', content: String(i) })
    expect(state.rowPatchLog).toHaveLength(ROW_PATCH_LOG_CAP)
    expect(rowPatchesSince(state, 'a', 0)).toBeNull()
    expect(rowPatchesSince(state, 'a', 5)).toHaveLength(ROW_PATCH_LOG_CAP)
  })

  it('finds a tool row by tool_call_id, newest first, then any row by mid, then by ts', () => {
    const rows = [
      { role: 'tool', content: 'old', ts: 't1', meta: { tool_call_id: 'c1' } },
      { role: 'tool', content: 'new', ts: 't2', meta: { tool_call_id: 'c1' } },
      { role: 'assistant', content: 'a', ts: 't3', meta: { mid: 'm-3' } },
      { role: 'user', content: 'u', ts: 't4' },
    ] as ChatMessage[]
    applyRowPatch(rows, { slot: 's', tcid: 'c1', content: 'done' })
    applyRowPatch(rows, { slot: 's', mid: 'm-3', meta: { k: 1 } })
    applyRowPatch(rows, { slot: 's', ts: 't4', content: 'edited' })
    expect(rows.map(m => m.content)).toEqual(['old', 'done', 'a', 'edited'])
    expect(rows[2].meta).toEqual({ mid: 'm-3', k: 1 })
  })
})

describe('withReplayedPatches', () => {
  const read = () => ({ messages: [{ role: 'assistant', content: 'old', ts: 't1', meta: { mid: 'm-1' } }] as ChatMessage[] })

  it('returns the read untouched when nothing landed, and patched copies when something did', () => {
    const state = chat()
    const untouched = read()
    expect(withReplayedPatches(state, 'a', 0, untouched)).toBe(untouched)
    logRowPatch(state, { slot: 'a', mid: 'm-1', content: 'new' })
    const original = read()
    const out = withReplayedPatches(state, 'a', 0, original)
    expect(out.messages[0].content).toBe('new')
    expect(original.messages[0].content).toBe('old')
  })

  it('throws once the log no longer holds every patch since the mark', () => {
    const state = chat()
    for (let i = 0; i < ROW_PATCH_LOG_CAP + 1; i++) logRowPatch(state, { slot: 'a', mid: 'm-1', content: String(i) })
    expect(() => withReplayedPatches(state, 'a', 0, read())).toThrow()
  })
})
