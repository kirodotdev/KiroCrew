/**
 * A refetch that returns the config already held must not re-render or hand a
 * new array to the callers of the agent roster and model-list hooks.
 *
 * The sidebar's per-row shell memo depends on the roster arrays, so a fresh
 * array on every `refresh` frame re-rendered every mounted session row. The
 * model hooks spread the React Query result, which subscribes the caller to
 * every fetch-state flip, and rebuilt the hidden-model list on each render.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'

const fetchAvailableModels = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    agentCatalog: vi.fn(),
    kirocrewConfig: vi.fn(() => Promise.resolve({})),
    dashboardConfig: vi.fn(),
  },
}))

vi.mock('../providers', () => ({
  useProvider: () => ({ id: 'test-provider', fetchAvailableModels }),
}))

const { api } = await import('../api/client')
const { useAgents } = await import('../hooks/useAgents')
const { useAvailableModelsQuery } = await import('../hooks/useAvailableModels')
const { useModelPickerHiddenModelsQuery } = await import('../hooks/useInteractiveModels')

const mockApi = api as unknown as {
  agentCatalog: ReturnType<typeof vi.fn>
  dashboardConfig: ReturnType<typeof vi.fn>
}

// React Query hands observer updates to its notify manager, which flushes them
// on a zero-delay timer. Wait for that flush so a re-render it causes is
// counted before the assertion runs.
const flushNotifications = () => new Promise(resolve => setTimeout(resolve, 0))

function harness() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children)
  return { client, wrapper }
}

// Each call returns a fresh deep copy, as a JSON response does.
const roster = (extra: string[] = []) => ({
  agents: [
    { name: 'kirocrew', selection_kind: 'template', scope: 'global' },
    { name: 'reviewer', selection_kind: 'member', kiro_agent: 'reviewer', memory_store: 'reviewer' },
    ...extra.map(name => ({ name, selection_kind: 'template', scope: 'global' })),
  ],
  default_agent: 'kirocrew',
})

beforeEach(() => {
  vi.clearAllMocks()
})

describe('useAgents keeps an unchanged roster', () => {
  it('returns the same arrays after a refresh that repeats the roster', async () => {
    mockApi.agentCatalog.mockImplementation(() => Promise.resolve(roster()))
    const { wrapper } = harness()
    const view = renderHook(({ trig }: { trig: number }) => useAgents(trig, 'chat-1', '/repo'), {
      wrapper, initialProps: { trig: 0 },
    })
    await waitFor(() => expect(view.result.current.agents).toHaveLength(2))
    const before = view.result.current

    view.rerender({ trig: 1 })
    await waitFor(() => expect(mockApi.agentCatalog).toHaveBeenCalledTimes(2))
    await act(async () => {})

    const after = view.result.current
    expect(after.agents).toBe(before.agents)
    expect(after.displayAgents).toBe(before.displayAgents)
    expect(after.choices).toBe(before.choices)
    expect(after.displayChoices).toBe(before.displayChoices)
  })

  it('takes the new roster when a refresh changes it', async () => {
    mockApi.agentCatalog.mockImplementation(() => Promise.resolve(roster()))
    const { wrapper } = harness()
    const view = renderHook(({ trig }: { trig: number }) => useAgents(trig, 'chat-1', '/repo'), {
      wrapper, initialProps: { trig: 0 },
    })
    await waitFor(() => expect(view.result.current.agents).toHaveLength(2))
    const before = view.result.current.displayAgents

    mockApi.agentCatalog.mockImplementation(() => Promise.resolve(roster(['planner'])))
    view.rerender({ trig: 1 })

    await waitFor(() => expect(view.result.current.displayAgents).toHaveLength(3))
    expect(view.result.current.displayAgents).not.toBe(before)
  })
})

describe('model hooks re-render only for what the caller reads', () => {
  it('does not re-render a data-only caller when the model list refetches unchanged', async () => {
    fetchAvailableModels.mockImplementation(() => Promise.resolve([{ name: 'auto', description: '' }, { name: 'model-a' }]))
    const { client, wrapper } = harness()
    let renders = 0
    const view = renderHook(() => { renders++; return useAvailableModelsQuery().data }, { wrapper })
    await waitFor(() => expect(view.result.current).toHaveLength(2))
    const list = view.result.current
    const settled = renders

    await act(async () => { await client.refetchQueries({ queryKey: ['available-models'] }); await flushNotifications() })

    expect(fetchAvailableModels).toHaveBeenCalledTimes(2)
    expect(renders).toBe(settled)
    expect(view.result.current).toBe(list)
  })

  it('keeps the hidden-model list across renders and an unchanged refetch', async () => {
    mockApi.dashboardConfig.mockImplementation(() => Promise.resolve({ model_picker_hidden_models: [' model-a ', 'auto', 'model-b', 'model-a'] }))
    const { client, wrapper } = harness()
    let renders = 0
    const view = renderHook(() => { renders++; return useModelPickerHiddenModelsQuery().data }, { wrapper })
    await waitFor(() => expect(view.result.current).toEqual(['model-a', 'model-b']))
    const list = view.result.current

    view.rerender()
    expect(view.result.current).toBe(list)

    const settled = renders
    await act(async () => { await client.refetchQueries({ queryKey: ['dashboardConfig'] }); await flushNotifications() })

    expect(mockApi.dashboardConfig).toHaveBeenCalledTimes(2)
    expect(renders).toBe(settled)
    expect(view.result.current).toBe(list)
  })
})
