import { afterEach, describe, expect, it, vi } from 'vitest'
import React from 'react'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'
import { useKiroUsageReadout } from '../shell/topbar/kiroUsageReadout'

/**
 * The capsule's quiet "Sign in to Kiro" segment: on a harness with no balance
 * here, a build that offers Kiro sign-in says when none is stored. It is read
 * from Crew's own sign-in status, never from the usage read, which off KAS goes
 * through kiro-cli and fails there by construction.
 */

vi.mock('../api/client', () => ({
  api: {
    sessionsUsage: vi.fn(),
    kirocrewConfig: vi.fn(),
    kasLoginStatus: vi.fn(),
  },
}))

const offered = vi.hoisted(() => ({ value: true as boolean | undefined }))
vi.mock('../pages/developer/useKiroSignInOffered', () => ({
  useKiroSignInOffered: () => offered.value,
}))

async function mount(acp_backend: string, authenticated: boolean) {
  const { api } = await import('../api/client')
  vi.mocked(api).kirocrewConfig.mockResolvedValue({ agent: { acp_backend } } as never)
  // Off KAS the usage read is refused (503 before kiro-cli is ready).
  vi.mocked(api).sessionsUsage.mockRejectedValue(new Error('kiro_prerequisite_required'))
  vi.mocked(api).kasLoginStatus.mockResolvedValue({ authenticated } as never)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore()
  function Wrapper({ children }: { children: React.ReactNode }) {
    return <Provider store={store}><QueryClientProvider client={queryClient}><MemoryRouter>{children}</MemoryRouter></QueryClientProvider></Provider>
  }
  const view = renderHook(() => useKiroUsageReadout(), { wrapper: Wrapper })
  await waitFor(() => expect(queryClient.getQueryState(['kirocrewConfig'])?.status).toBe('success'))
  await waitFor(() => expect(queryClient.getQueryState(['kiro-usage'])?.status).toBe('error'))
  return { api, ...view }
}

describe('the capsule says "Sign in to Kiro" off the Kiro harnesses', () => {
  afterEach(() => {
    offered.value = true
    vi.clearAllMocks()
  })

  it('shows on Claude when no Kiro sign-in is stored', async () => {
    const { result } = await mount('claude', false)
    await waitFor(() => expect(result.current.kiroSignInPrompt).toBe(true))
    expect(result.current.usageSegmentHidden).toBe(true)
  })

  it('stays away once a Kiro sign-in is stored, and shows no balance', async () => {
    const { result, api } = await mount('claude', true)
    await waitFor(() => expect(vi.mocked(api).kasLoginStatus).toHaveBeenCalled())
    await waitFor(() => expect(result.current.kiroSignInPrompt).toBe(false))
    expect(result.current.usageSegmentHidden).toBe(true)
  })

  it('stays away on a build that offers no Kiro sign-in, and never reads it', async () => {
    offered.value = false
    const { result, api } = await mount('claude', false)
    expect(result.current.kiroSignInPrompt).toBe(false)
    expect(vi.mocked(api).kasLoginStatus).not.toHaveBeenCalled()
  })

  it('leaves a Kiro harness to its own credits segment', async () => {
    const { result, api } = await mount('kiro', false)
    expect(result.current.kiroSignInPrompt).toBe(false)
    expect(vi.mocked(api).kasLoginStatus).not.toHaveBeenCalled()
  })
})
