/**
 * `useChatProjectDir`: the project a chat transcript joins relative paths to.
 * Only a chat confirmed to run on this machine gets one; a remote chat's
 * project is a folder on another machine.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const env = vi.hoisted(() => ({ profile: null as string | null }))

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      chatSlotEnvironment: vi.fn(() => Promise.resolve({
        profile: env.profile,
        profiles: env.profile ? [{ id: env.profile, name: 'box', workdir: '/w' }] : [],
      })),
    },
  }
})

import { api } from '../api/client'
import { useChatProjectDir, useSlotEnvironmentPlacement } from '../components/sessionEnvironmentHooks'

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

beforeEach(() => {
  env.profile = null
  vi.mocked(api.chatSlotEnvironment).mockClear()
})

describe('useChatProjectDir', () => {
  it('returns the project of a chat that runs here', async () => {
    const { result } = renderHook(() => useChatProjectDir({ key: 's1', project: '/home/u/proj', executor: 'local' }), { wrapper })
    await waitFor(() => expect(result.current).toBe('/home/u/proj'))
  })

  it('returns null for a chat placed on a remote environment', async () => {
    env.profile = 'devbox'
    const { result } = renderHook(() => ({
      dir: useChatProjectDir({ key: 's1', project: '/home/u/proj' }),
      placement: useSlotEnvironmentPlacement('s1'),
    }), { wrapper })
    // Wait for the placement read itself to settle, so the null is the remote
    // answer rather than a read still in flight.
    await waitFor(() => expect(result.current.placement.remote).toBeTruthy())
    expect(result.current.dir).toBeNull()
  })

  it('returns null for a remote-executor chat without reading placement', () => {
    const { result } = renderHook(() => useChatProjectDir({ key: 's1', project: '/home/u/proj', executor: 'remote' }), { wrapper })
    expect(result.current).toBeNull()
    expect(api.chatSlotEnvironment).not.toHaveBeenCalled()
  })

  it('returns null for a chat with no project or no slot', async () => {
    const { result } = renderHook(() => useChatProjectDir({ key: 's1' }), { wrapper })
    await waitFor(() => expect(api.chatSlotEnvironment).toHaveBeenCalled())
    expect(result.current).toBeNull()
    const none = renderHook(() => useChatProjectDir(undefined), { wrapper })
    expect(none.result.current).toBeNull()
  })
})
