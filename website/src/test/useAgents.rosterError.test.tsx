/**
 * `useAgents` must make a FAILED roster fetch distinguishable from an install
 * that genuinely has one agent, and it must offer a way back (#5990).
 *
 * The hook used to swallow the rejection (`.catch(() => {})`) and return only
 * `{ agents, defaultAgent }`, so a failed fetch and a short roster were the same
 * observation to every caller. On a surface that passes a CONSTANT
 * `refreshTrigger` — `SchedulePage` passes `0`, so the effect's deps never
 * change — that left the roster empty for the whole life of the mount with no
 * error and no retry: the reported symptom, arrived at silently.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { renderHookWithProviders } from './helpers'
import { useAgents } from '../hooks/useAgents'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    kirocrewAgents: vi.fn(),
    syncKirocrewAgents: vi.fn().mockResolvedValue({}),
  },
}))

const roster = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'built-in', source: 'kirocrew' },
  { name: 'oncall', kiro_agent: 'oncall-agent', workspace: 'oncall', memory_store: 'oncall-kb', description: 'paging', source: 'package' },
]

const agentsApi = vi.mocked(api.kirocrewAgents)

describe('useAgents roster failure is reportable and retryable (#5990)', () => {
  beforeEach(() => {
    vi.mocked(api.syncKirocrewAgents).mockResolvedValue({} as never)
    agentsApi.mockReset()
  })

  it('reports a failed fetch instead of returning a silently empty roster', async () => {
    agentsApi.mockRejectedValue(new Error('gateway restarting'))

    const { result } = renderHookWithProviders(() => useAgents(0))

    await waitFor(() => expect(result.current.error).toBe(true))
    // The failure does not throw — the surface that asked still renders.
    expect(result.current.agents).toEqual([])
  })

  it('reload() re-fetches, so a constant refreshTrigger is no longer a dead end', async () => {
    agentsApi.mockRejectedValueOnce(new Error('gateway restarting'))

    const { result } = renderHookWithProviders(() => useAgents(0))
    await waitFor(() => expect(result.current.error).toBe(true))

    // The retry the schedule form offers. `refreshTrigger` cannot do this: it is
    // the literal 0 there, so the effect would never run again.
    agentsApi.mockResolvedValue({ agents: roster, default_agent: 'kirocrew' } as never)
    act(() => { result.current.reload() })

    await waitFor(() => expect(result.current.agents).toHaveLength(2))
    expect(result.current.error).toBe(false)
    expect(result.current.defaultAgent).toBe('kirocrew')
  })

  it('keeps the roster it already holds when a later refresh fails', async () => {
    agentsApi.mockResolvedValue({ agents: roster, default_agent: 'kirocrew' } as never)

    const { result } = renderHookWithProviders(() => useAgents(0))
    await waitFor(() => expect(result.current.agents).toHaveLength(2))

    agentsApi.mockRejectedValue(new Error('transient'))
    act(() => { result.current.reload() })

    await waitFor(() => expect(result.current.error).toBe(true))
    // A failed REFRESH must not empty a list the user can still act on.
    expect(result.current.agents).toHaveLength(2)
  })

  it('reports a retry as in flight and settles it even when the retry fails again', async () => {
    agentsApi.mockRejectedValue(new Error('still down'))

    const { result } = renderHookWithProviders(() => useAgents(0))
    await waitFor(() => expect(result.current.error).toBe(true))
    expect(result.current.reloading).toBe(false)

    // The state the UX review named: `setError(true)` over an already-true value
    // bails out of re-rendering, so without `reloading` a second failure is
    // invisible and the Retry button looks dead during the outage it exists for.
    act(() => { result.current.reload() })
    expect(result.current.reloading).toBe(true)

    await waitFor(() => expect(result.current.reloading).toBe(false))
    expect(result.current.error).toBe(true)
  })
})
