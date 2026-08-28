import { describe, it, expect, vi, beforeEach } from 'vitest'
import React from 'react'
import { act, waitFor } from '@testing-library/react'
import { renderHookWithProviders, createTestStore } from './helpers'
import { useAgentSync } from '../hooks/useAgentSync'
import { sseSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

// Issue #6495: slots created before the default agent was stamped into slot
// metadata (PR #5699) carry agent:'' and were labeled with the literal
// 'default' in the agents rail. The label must resolve to the alias that will
// actually answer — the configured default agent — and degrade to the literal
// 'default' only while that value has not loaded.
//
// The hook reads GET /api/config/default-agent (api.defaultAgent), NOT
// useAgents(0): the latter fires the owner-only, config-writing
// POST /api/agents/sync per mount, which the view-only Worlds surfaces must
// not pay. A test failing on a missing defaultAgent mock is the pin for that.

vi.mock('../api/client', () => ({
  api: {
    defaultAgent: vi.fn(),
    crons: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue([]),
  },
}))

const mkSlot = (overrides: Partial<ChatSlot> = {}): ChatSlot => ({
  key: 'chat-1',
  title: 'Legacy session',
  messages: 3,
  running: false,
  agent: '',
  ...overrides,
} as ChatSlot)

const storeWithSlots = (slots: ChatSlot[]) => {
  const store = createTestStore()
  act(() => { store.dispatch(sseSlots(slots)) })
  return store
}

describe('useAgentSync legacy empty-agent slot label (#6495)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('labels an agent-less slot with the resolved default alias', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).defaultAgent.mockResolvedValue({ default_agent: 'atlas' })

    const store = storeWithSlots([mkSlot()])
    const { result } = renderHookWithProviders(() => useAgentSync(), { store })

    await waitFor(() => {
      const slot = result.current.agents.find(a => a.id === 'slot-chat-1')
      expect(slot?.label).toBe('atlas · default')
    })
  })

  it('keeps a pinned slot agent even when a default is resolved', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).defaultAgent.mockResolvedValue({ default_agent: 'atlas' })

    // A legacy control row in the same store proves the resolved default has
    // FLUSHED into the labels before the pinned row is asserted — without it
    // this test would pass on a render that never applied the fetch at all.
    const store = storeWithSlots([mkSlot(), mkSlot({ key: 'chat-2', agent: 'coder' })])
    const { result } = renderHookWithProviders(() => useAgentSync(), { store })

    await waitFor(() => {
      expect(result.current.agents.find(a => a.id === 'slot-chat-1')?.label).toBe('atlas · default')
    })
    expect(result.current.agents.find(a => a.id === 'slot-chat-2')?.label).toBe('coder')
  })

  it("degrades to the literal 'default' while the value has not loaded", async () => {
    const { api } = await import('../api/client')
    // Never resolves: the pre-load frame is the state under test.
    vi.mocked(api).defaultAgent.mockReturnValue(new Promise(() => {}))

    const store = storeWithSlots([mkSlot()])
    const { result } = renderHookWithProviders(() => useAgentSync(), { store })

    const slot = result.current.agents.find(a => a.id === 'slot-chat-1')
    expect(slot?.label).toBe('default')
  })

  it("degrades to the literal 'default' when the server reports no default", async () => {
    const { api } = await import('../api/client')
    // Deferred resolution the test flushes EXPLICITLY, so the assertion runs
    // against a state that has provably absorbed the empty response rather
    // than against the identical pre-fetch frame.
    let resolveFetch!: (v: { default_agent: string }) => void
    vi.mocked(api).defaultAgent.mockReturnValue(new Promise(r => { resolveFetch = r }))

    const store = storeWithSlots([mkSlot()])
    const { result } = renderHookWithProviders(() => useAgentSync(), { store })

    await act(async () => { resolveFetch({ default_agent: '' }) })
    const slot = result.current.agents.find(a => a.id === 'slot-chat-1')
    expect(slot?.label).toBe('default')
  })

  it("re-resolves the label when the shared ['default-agent'] query is invalidated", async () => {
    // A long-lived Worlds/popout surface must not pin a stale alias after the
    // configured default changes: useWebSocket invalidates ['default-agent']
    // on refresh events, so the hook's read MUST live on that shared key.
    // This test drives the invalidation directly and asserts the label moves.
    const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
    const { Provider } = await import('react-redux')
    const { renderHook } = await import('@testing-library/react')
    const { api } = await import('../api/client')

    vi.mocked(api).defaultAgent.mockResolvedValue({ default_agent: 'atlas' })
    const store = storeWithSlots([mkSlot()])
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={queryClient}>
        <Provider store={store}>{children}</Provider>
      </QueryClientProvider>
    )
    const { result } = renderHook(() => useAgentSync(), { wrapper })

    await waitFor(() => {
      expect(result.current.agents.find(a => a.id === 'slot-chat-1')?.label).toBe('atlas · default')
    })

    vi.mocked(api).defaultAgent.mockResolvedValue({ default_agent: 'nova' })
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ['default-agent'] })
    })
    await waitFor(() => {
      expect(result.current.agents.find(a => a.id === 'slot-chat-1')?.label).toBe('nova · default')
    })
  })
})
