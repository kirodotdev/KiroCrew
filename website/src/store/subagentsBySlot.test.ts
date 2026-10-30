import { describe, it, expect } from 'vitest'
import { selectSidebarStartedSubagentCounts, selectSubagentsBySlot } from './chat/subagents'
import type { RootState } from './index'

describe('sub-agent counts read per-slot sub-agent maps', () => {
  it('a tool row that replaces slotActivity but not its subagents map keeps the result', () => {
    const subs = { s1: { id: 's1', status: 'running' } }
    const activeSubs = {}
    const chat = (toolLog: unknown[]) => ({
      activeSlot: null, subagents: activeSubs, subagentQueued: {},
      slotActivity: { bg: { toolLog, subagents: subs } },
    })
    const a = { chat: chat([]) } as unknown as RootState
    const b = { chat: chat([{ tool_call_id: 't1' }]) } as unknown as RootState
    expect(selectSubagentsBySlot(b)).toBe(selectSubagentsBySlot(a))
    const counts = selectSidebarStartedSubagentCounts(a)
    expect(selectSidebarStartedSubagentCounts(b)).toBe(counts)
    expect(counts).toEqual({ bg: 1 })
  })
})
