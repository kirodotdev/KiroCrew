/**
 * `selectSlotSubagents` — the slot-aware read-only twin of the internal
 * `getSlotSubs`, so the Activity panel can subscribe to the subagent map
 * directly instead of ChatPage holding the subscription and passing it down.
 *
 * `sseSubagentChunk` mutates this map per streamed sub-agent chunk, so a
 * ChatPage-level subscription would re-render the whole page for a panel that is
 * closed by default. These tests pin the selector's slot routing and its
 * reference stability, which is what keeps the subscription cheap.
 */
import { describe, it, expect } from 'vitest'
import { selectSlotSubagents, selectSlotToolLog } from '../store/chatSlice'
import type { RootState } from '../store'
import type { SubagentActivity, ToolActivity } from '../types'

const sub = (id: string, status = 'running'): SubagentActivity =>
  ({ id, status, task: 't', agent: 'a', streaming: '', startedAt: 0 } as unknown as SubagentActivity)

function state(over: Partial<RootState['chat']>): RootState {
  return {
    chat: {
      activeSlot: 'slot-active',
      subagents: {},
      toolLog: [] as ToolActivity[],
      slotActivity: {},
      ...over,
    },
  } as unknown as RootState
}

describe('selectSlotSubagents', () => {
  it('returns the global active mirror for the active slot', () => {
    const active = { a1: sub('a1') }
    const s = state({ subagents: active })
    expect(selectSlotSubagents(s, 'slot-active')).toBe(active)
  })

  it('returns the per-slot map for a background slot', () => {
    const bg = { b1: sub('b1') }
    const s = state({ subagents: {}, slotActivity: { 'slot-bg': { subagents: bg } } as never })
    expect(selectSlotSubagents(s, 'slot-bg')).toBe(bg)
  })

  it('falls back to the active mirror for a null slot', () => {
    // Mirrors selectSlotToolLog: a null slot means "whatever is active".
    const active = { a1: sub('a1') }
    const s = state({ subagents: active })
    expect(selectSlotSubagents(s, null)).toBe(active)
  })

  it('returns a STABLE empty object for an unknown background slot', () => {
    // A fresh {} per call would defeat the subscription it was added for —
    // every store change would look like a change to this slot's subagents.
    const s = state({ slotActivity: {} })
    const first = selectSlotSubagents(s, 'slot-missing')
    const second = selectSlotSubagents(s, 'slot-missing')
    expect(first).toEqual({})
    expect(first).toBe(second)
  })

  it('returns a stable empty object for a background slot with no subagents key', () => {
    const s = state({ slotActivity: { 'slot-bg': {} } as never })
    expect(selectSlotSubagents(s, 'slot-bg')).toBe(selectSlotSubagents(s, 'slot-bg'))
  })

  it('is reference-stable across repeated calls on unchanged state', () => {
    const s = state({ subagents: { a1: sub('a1') } })
    expect(selectSlotSubagents(s, 'slot-active')).toBe(selectSlotSubagents(s, 'slot-active'))
  })

  it('routes the same way selectSlotToolLog does', () => {
    // The two selectors are siblings; a divergence in slot routing would make the
    // Activity panel show one slot's tools next to another slot's agents.
    const s = state({
      subagents: { a1: sub('a1') },
      toolLog: [{ type: 'tool', text: 'x' }] as unknown as ToolActivity[],
      slotActivity: { 'slot-bg': { subagents: { b1: sub('b1') }, toolLog: [] } } as never,
    })
    expect(selectSlotSubagents(s, 'slot-active')).toBe(s.chat.subagents)
    expect(selectSlotToolLog(s, 'slot-active')).toBe(s.chat.toolLog)
    expect(selectSlotSubagents(s, 'slot-bg')).not.toBe(s.chat.subagents)
    expect(selectSlotToolLog(s, 'slot-bg')).not.toBe(s.chat.toolLog)
  })
})
