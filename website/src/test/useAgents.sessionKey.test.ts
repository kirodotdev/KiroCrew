/**
 * The agents picker must scope its fetch to the CHAT SLOT it belongs to.
 *
 * Regression guard for the bug where project-local agents (the
 * `<project>/.kiro/agents/*.json` discovered by agent_discovery) never showed
 * up in the picker. The server resolves them through
 * `active_project_dir(state, session_key)`, which:
 *
 *   1. uses the slot named by session_key, when it has a project;
 *   2. else uses the single project shared by every slot that has one;
 *   3. else returns None.
 *
 * `kirocrewAgents()` sent no `X-Session-Key` at all, so step 1 could never
 * match. With two chats open on different projects step 2 fails closed by
 * design, and every project-scoped row silently disappeared. The backend was
 * correct the whole time; only the caller was unscoped.
 *
 * These tests pin the contract at the seam that broke: the hook forwards its
 * session key to the client, and re-fetches when the focused slot changes.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { useAgents } from '../hooks/useAgents'

vi.mock('../api/client', () => ({
  api: {
    kirocrewAgents: vi.fn(),
    syncKirocrewAgents: vi.fn(),
  },
}))

const { api } = await import('../api/client')
const mockApi = api as unknown as {
  kirocrewAgents: ReturnType<typeof vi.fn>
  syncKirocrewAgents: ReturnType<typeof vi.fn>
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.syncKirocrewAgents.mockResolvedValue({})
  mockApi.kirocrewAgents.mockResolvedValue({
    agents: [
      { name: 'kirocrew', scope: 'global' },
      { name: 'project-agent', scope: 'project' },
    ],
    default_agent: 'kirocrew',
  })
})

describe('useAgents session scoping', () => {
  it('forwards the slot key so the server can resolve project-local agents', async () => {
    renderHook(() => useAgents(0, 'chat-2-1786309747'))

    await waitFor(() => expect(mockApi.kirocrewAgents).toHaveBeenCalled())
    expect(mockApi.kirocrewAgents).toHaveBeenCalledWith('chat-2-1786309747')
  })

  it('exposes project-scoped agents returned for that slot', async () => {
    const { result } = renderHook(() => useAgents(0, 'chat-2-1786309747'))

    await waitFor(() => expect(result.current.agents).toHaveLength(2))
    expect(result.current.agents.map(a => a.name)).toContain('project-agent')
  })

  it('re-fetches when the focused slot changes, since project scope is per slot', async () => {
    const { rerender } = renderHook(
      ({ sk }: { sk: string }) => useAgents(0, sk),
      { initialProps: { sk: 'chat-1' } },
    )

    await waitFor(() => expect(mockApi.kirocrewAgents).toHaveBeenCalledWith('chat-1'))

    rerender({ sk: 'chat-2' })
    await waitFor(() => expect(mockApi.kirocrewAgents).toHaveBeenCalledWith('chat-2'))
  })

  it('clears the previous slot\'s roster while the new scope\'s fetch is in flight', async () => {
    // A stale project agent selected in the switch window would be stored
    // against the NEW slot and reset its project — the roster must be empty
    // from the moment the key changes until that key's response arrives.
    let resolveSecond: (v: unknown) => void = () => {}
    const { result, rerender } = renderHook(
      ({ sk }: { sk: string }) => useAgents(0, sk),
      { initialProps: { sk: 'chat-1' } },
    )
    await waitFor(() => expect(result.current.agents).toHaveLength(2))

    mockApi.kirocrewAgents.mockImplementationOnce(
      () => new Promise(res => { resolveSecond = res }),
    )
    rerender({ sk: 'chat-2' })

    // In-flight: the old roster must be gone, not selectable.
    expect(result.current.agents).toHaveLength(0)

    resolveSecond({ agents: [{ name: 'other-project-agent', scope: 'project' }], default_agent: 'kirocrew' })
    await waitFor(() => expect(result.current.agents).toHaveLength(1))
    expect(result.current.agents[0].name).toBe('other-project-agent')
  })

  it('does not clear the roster on a same-slot refresh (no flicker)', async () => {
    const { result, rerender } = renderHook(
      ({ trig }: { trig: number }) => useAgents(trig, 'chat-1'),
      { initialProps: { trig: 0 } },
    )
    await waitFor(() => expect(result.current.agents).toHaveLength(2))

    mockApi.kirocrewAgents.mockImplementationOnce(() => new Promise(() => {}))
    rerender({ trig: 1 })

    // Same slot, refresh only: the list stays while the refetch is in flight.
    expect(result.current.agents).toHaveLength(2)
  })

  it('omits the key on surfaces with no slot context (Channels, Schedule)', async () => {
    renderHook(() => useAgents(0))

    await waitFor(() => expect(mockApi.kirocrewAgents).toHaveBeenCalled())
    expect(mockApi.kirocrewAgents).toHaveBeenCalledWith(undefined)
  })
})
