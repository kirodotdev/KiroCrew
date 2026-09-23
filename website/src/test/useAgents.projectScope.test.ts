/**
 * The agents picker must re-fetch when its slot is pointed at a DIFFERENT
 * project, not just when the focused slot changes.
 *
 * Regression guard for the bug where project-local agents
 * (`<project>/.kiro/agents/*.json`) stayed missing from the picker after the
 * user set the slot's project. The reported symptom was oddly specific: the
 * agents appeared only after opening Manage Agents, navigating back, and
 * reopening the selector.
 *
 * Root cause was entirely client-side. `useAgents` re-ran only on
 * `[refreshTrigger, sessionKey]`, and setting a project changes NEITHER — it is
 * the same slot, and no refresh is dispatched. So the roster fetched at mount
 * (taken before any project was recorded, when the server correctly resolved no
 * project scope) was what the dropdown kept showing. The workaround worked
 * because leaving the page unmounts ChatPage; returning remounts it and the
 * effect re-runs against the now-recorded project. It was a remount, not a
 * cache warm — which is why reopening the dropdown alone never helped.
 *
 * The server was correct throughout: `GET /api/agents` rescans the project
 * directory on every request. Only the client's notion of when to ask was wrong.
 *
 * These tests pin the fetch's identity as (slot, project) rather than slot
 * alone. `projectDir` is deliberately NOT sent to the API — the server derives
 * project scope from the session key — so it must change the fetch's *identity*
 * without changing its *arguments*.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { useAgents } from '../hooks/useAgents'

vi.mock('../api/client', () => ({
  api: {
    agentCatalog: vi.fn(),
  },
}))

const { api } = await import('../api/client')
const mockApi = api as unknown as {
  agentCatalog: ReturnType<typeof vi.fn>
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.agentCatalog.mockResolvedValue({
    agents: [{ name: 'kirocrew', scope: 'global' }],
    default_agent: 'kirocrew',
  })
})

describe('useAgents project scoping', () => {
  it('re-fetches when the same slot is pointed at a project', async () => {
    // The reported bug: mount with no project, then set one. Before the fix the
    // effect never re-ran, so the project's agents never arrived.
    const { rerender } = renderHook(
      ({ dir }: { dir?: string }) => useAgents(0, 'chat-1', dir),
      { initialProps: { dir: undefined as string | undefined } },
    )
    await waitFor(() => expect(mockApi.agentCatalog).toHaveBeenCalledTimes(1))

    mockApi.agentCatalog.mockResolvedValue({
      agents: [
        { name: 'kirocrew', scope: 'global' },
        { name: 'project-agent', scope: 'project' },
      ],
      default_agent: 'kirocrew',
    })
    rerender({ dir: '/repo/service' })

    await waitFor(() => expect(mockApi.agentCatalog).toHaveBeenCalledTimes(2))
  })

  it('surfaces the project-scoped agents once the project is set', async () => {
    const { result, rerender } = renderHook(
      ({ dir }: { dir?: string }) => useAgents(0, 'chat-1', dir),
      { initialProps: { dir: undefined as string | undefined } },
    )
    await waitFor(() => expect(result.current.agents).toHaveLength(1))

    mockApi.agentCatalog.mockResolvedValue({
      agents: [
        { name: 'kirocrew', scope: 'global' },
        { name: 'project-agent', scope: 'project' },
      ],
      default_agent: 'kirocrew',
    })
    rerender({ dir: '/repo/service' })

    await waitFor(() => expect(result.current.agents).toHaveLength(2))
    expect(result.current.agents.map(a => a.name)).toContain('project-agent')
  })

  it('re-fetches when the slot moves between two projects', async () => {
    const { rerender } = renderHook(
      ({ dir }: { dir: string }) => useAgents(0, 'chat-1', dir),
      { initialProps: { dir: '/repo/one' } },
    )
    await waitFor(() => expect(mockApi.agentCatalog).toHaveBeenCalledTimes(1))

    rerender({ dir: '/repo/two' })
    await waitFor(() => expect(mockApi.agentCatalog).toHaveBeenCalledTimes(2))
  })

  it('still sends only the session key — the server derives project scope', async () => {
    // projectDir participates in the fetch's identity, not its arguments. If it
    // ever became a request parameter, the backend contract would have changed.
    const { rerender } = renderHook(
      ({ dir }: { dir: string }) => useAgents(0, 'chat-1', dir),
      { initialProps: { dir: '/repo/one' } },
    )
    await waitFor(() => expect(mockApi.agentCatalog).toHaveBeenCalledTimes(1))
    rerender({ dir: '/repo/two' })
    await waitFor(() => expect(mockApi.agentCatalog).toHaveBeenCalledTimes(2))

    for (const call of mockApi.agentCatalog.mock.calls) {
      expect(call).toEqual(['chat-1'])
    }
  })

  it('clears the old project\'s roster while the new project\'s fetch is in flight', async () => {
    // Same hazard the slot-switch guard covers: a stale project agent selected
    // in this window would be stored against the slot and reset its project.
    let resolveSecond: (v: unknown) => void = () => {}
    const { result, rerender } = renderHook(
      ({ dir }: { dir: string }) => useAgents(0, 'chat-1', dir),
      { initialProps: { dir: '/repo/one' } },
    )
    await waitFor(() => expect(result.current.agents).toHaveLength(1))

    mockApi.agentCatalog.mockImplementationOnce(
      () => new Promise(res => { resolveSecond = res }),
    )
    rerender({ dir: '/repo/two' })

    expect(result.current.agents).toHaveLength(0)

    resolveSecond({ agents: [{ name: 'two-agent', scope: 'project' }], default_agent: 'kirocrew' })
    await waitFor(() => expect(result.current.agents).toHaveLength(1))
    expect(result.current.agents[0].name).toBe('two-agent')
  })

  it('does not clear the roster on a same-project refresh (no flicker)', async () => {
    const { result, rerender } = renderHook(
      ({ trig }: { trig: number }) => useAgents(trig, 'chat-1', '/repo/one'),
      { initialProps: { trig: 0 } },
    )
    await waitFor(() => expect(result.current.agents).toHaveLength(1))

    mockApi.agentCatalog.mockImplementationOnce(() => new Promise(() => {}))
    rerender({ trig: 1 })

    expect(result.current.agents).toHaveLength(1)
  })

  it('treats an omitted project as its own scope, without refetch churn', async () => {
    // Surfaces with no slot context (Channels, Schedule) pass neither key nor
    // project; a re-render must not look like a scope change.
    const { rerender } = renderHook(() => useAgents(0))
    await waitFor(() => expect(mockApi.agentCatalog).toHaveBeenCalledTimes(1))

    rerender()
    expect(mockApi.agentCatalog).toHaveBeenCalledTimes(1)
  })
  it('never issues the enrolling sync write: listing is a read', async () => {
    // The hook used to POST `/api/agents/sync` before listing, and that write
    // enrolled every discovered template as a crew member with its own memory.
    // Reading the catalog must not grow the Crew Members roster as a side effect,
    // so the client object is given NO sync method: reaching for one would throw.
    const { result } = renderHook(() => useAgents(0, 'chat-1', '/repo/service'))
    await waitFor(() => expect(result.current.agents).toHaveLength(1))
    expect('syncKirocrewAgents' in mockApi).toBe(false)
    expect(mockApi.agentCatalog).toHaveBeenCalledTimes(1)
  })
})
