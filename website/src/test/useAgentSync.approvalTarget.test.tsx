import { describe, it, expect, vi } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { renderHookWithProviders, createTestStore } from './helpers'
import { useAgentSync } from '../hooks/useAgentSync'
import { sseSlots } from '../store/dashboardSlice'
import type { ChatSlot, PendingApproval } from '../types'

// The scene decides a slot's pending approval through the owner-bound target
// built here from what the slot sent with it, never through the bare id.

vi.mock('../api/client', () => ({
  api: {
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: 'atlas' }),
    crons: vi.fn().mockResolvedValue({ jobs: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
}))

const slotWith = (key: string, info: Partial<PendingApproval>): ChatSlot => ({
  key, title: key, messages: 1, running: true, agent: 'atlas', pending_approval: true,
  pending_approval_info: { tool: 'shell', tool_input: '', tool_kind: '', request_id: 'req-1', ...info },
} as ChatSlot)

const targetsOf = (slots: ChatSlot[]) => {
  const store = createTestStore()
  act(() => { store.dispatch(sseSlots(slots)) })
  const { result } = renderHookWithProviders(() => useAgentSync(), { store })
  return () => Object.fromEntries(result.current.agents.map(a => [a.id, a.pendingApproval?.target]))
}

describe('useAgentSync pending approval target', () => {
  it('binds each origin to its owner and instance, and names nothing without one', async () => {
    const read = targetsOf([
      slotWith('chat-n', { origin: 'native', request_mid: 'mid-1' }),
      slotWith('chat-c', { origin: 'coordinator', request_instance: 'inst-1' }),
      slotWith('chat-old', { origin: 'coordinator' }),
      slotWith('chat-legacy', {}),
    ])
    await waitFor(() => expect(read()).toEqual({
      'slot-chat-n': { origin: 'native', id: 'req-1', slot: 'chat-n', mid: 'mid-1' },
      'slot-chat-c': { origin: 'coordinator', id: 'req-1', slot: 'chat-c', instance: 'inst-1' },
      'slot-chat-old': null,
      'slot-chat-legacy': null,
    }))
  })
})
