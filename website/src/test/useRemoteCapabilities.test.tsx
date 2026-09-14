import { act, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import {
  REMOTE_CAPABILITIES_RECOVERY_INTERVAL_MS,
  remoteCapabilitiesRefetchInterval,
  useRemoteCapabilities,
} from '../hooks/useRemoteCapabilities'
import type { ChatSlot, RemoteCrewCapabilities } from '../types'

vi.mock('../api/client', () => ({
  api: {
    instancesCapabilities: vi.fn(),
  },
}))

const peerSlot = {
  executor: 'remote',
  instance_id: 'remote-1',
} as ChatSlot

function capabilities(overrides: Partial<RemoteCrewCapabilities> = {}): RemoteCrewCapabilities {
  return {
    instance_id: 'remote-1',
    version: '1.0.0',
    local_version: '1.0.0',
    version_match: true,
    agents: [],
    default_agent: '',
    models: [],
    effort_levels: [],
    workspaces: [],
    default_workspace: '',
    unavailable: {},
    ...overrides,
  }
}

function wrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>
  }
}

describe('useRemoteCapabilities', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.mocked(api.instancesCapabilities).mockReset()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('retries a partial same-version response until the remote model list recovers', async () => {
    vi.mocked(api.instancesCapabilities)
      .mockResolvedValueOnce(capabilities({ unavailable: { models: 'capability_unreachable' } }))
      .mockResolvedValueOnce(capabilities({
        models: [{
          model_name: 'served-model',
          display_name: 'Served model',
          description: '',
          context_window: 200_000,
        }],
      }))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const { result } = renderHook(
      () => useRemoteCapabilities(peerSlot),
      { wrapper: wrapper(client) },
    )
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })

    expect(api.instancesCapabilities).toHaveBeenCalledTimes(1)
    expect(result.current.capabilities?.models).toEqual([])
    expect(result.current.modelsLoading).toBe(true)

    await act(async () => { await vi.advanceTimersByTimeAsync(8_000) })

    expect(api.instancesCapabilities).toHaveBeenCalledTimes(2)

    client.clear()
  })

  it('settles the loading state when the remote model list is healthy', async () => {
    vi.mocked(api.instancesCapabilities).mockResolvedValueOnce(capabilities({
      models: [{
        model_name: 'served-model',
        display_name: 'Served model',
        description: '',
        context_window: 200_000,
      }],
    }))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const { result } = renderHook(
      () => useRemoteCapabilities(peerSlot),
      { wrapper: wrapper(client) },
    )
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })

    expect(result.current.modelsLoading).toBe(false)
    expect(result.current.capabilities?.models.map(model => model.model_name)).toEqual(['served-model'])

    client.clear()
  })
})

describe('remoteCapabilitiesRefetchInterval', () => {
  it('uses the bounded recovery cadence for a partial compatible response', () => {
    expect(remoteCapabilitiesRefetchInterval({
      state: { data: capabilities({ unavailable: { models: 'capability_unreachable' } }) },
    })).toBe(REMOTE_CAPABILITIES_RECOVERY_INTERVAL_MS)
  })

  it('does not poll a disconnected or version-skewed peer', () => {
    expect(remoteCapabilitiesRefetchInterval({
      state: {
        data: capabilities({
          version_match: false,
          unavailable: { version: 'capability_unreachable', models: 'capability_unreachable' },
        }),
      },
    })).toBe(false)
  })
})
