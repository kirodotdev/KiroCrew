import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act } from '@testing-library/react'
import { renderHookWithProviders, createTestStore } from './helpers'
import { useAgentSync } from '../hooks/useAgentSync'
import { sseSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

// When more than 8 sessions exist, the Agent Worlds scene has only 8 desks.
// It must show the 8 MOST RECENTLY ACTIVE slots (newest first), not whichever
// 8 happen to sit first in the store's slot array (which was insertion order,
// so old sessions crowded out the ones actually in use).

vi.mock('../api/client', () => ({
  api: {
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: '' }),
    crons: vi.fn().mockResolvedValue({ jobs: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
}))

const mkSlot = (key: string, lastActivityTs: string): ChatSlot => ({
  key,
  title: key,
  messages: 1,
  running: false,
  agent: 'a',
  last_activity_ts: lastActivityTs,
} as ChatSlot)

const storeWithSlots = (slots: ChatSlot[]) => {
  const store = createTestStore()
  act(() => { store.dispatch(sseSlots(slots)) })
  return store
}

describe('useAgentSync recency ordering', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('keeps the 8 most recently active slots, newest first, out of 12', () => {
    // 12 slots inserted OLDEST-first (chat-1 oldest ... chat-12 newest), the
    // shape that used to strand the newest 4 off-screen.
    const slots = Array.from({ length: 12 }, (_, i) =>
      mkSlot(`chat-${i + 1}`, `2026-09-22T00:${String(i).padStart(2, '0')}:00Z`),
    )
    const { result } = renderHookWithProviders(() => useAgentSync(), {
      store: storeWithSlots(slots),
    })

    const ids = result.current.agents.map(a => a.id)
    expect(ids).toEqual([
      'slot-chat-12', 'slot-chat-11', 'slot-chat-10', 'slot-chat-9',
      'slot-chat-8', 'slot-chat-7', 'slot-chat-6', 'slot-chat-5',
    ])
  })

  it('falls back to last_ts then created when last_activity_ts is absent', () => {
    const store = storeWithSlots([
      { key: 'a', title: 'a', messages: 1, running: false, created: '2026-09-22T00:00:00Z' } as ChatSlot,
      { key: 'b', title: 'b', messages: 1, running: false, last_ts: '2026-09-22T05:00:00Z' } as ChatSlot,
    ])
    const { result } = renderHookWithProviders(() => useAgentSync(), { store })
    expect(result.current.agents.map(a => a.id)).toEqual(['slot-b', 'slot-a'])
  })
})
