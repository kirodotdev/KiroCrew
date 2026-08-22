import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// Mock api.branding at module level. Tests drive it per-case; the provider
// fetches through React Query (retry-enabled), so a failed first fetch followed
// by a success must still land directLocal true — that is the GPT finding this
// suite pins. We mock at the api layer, not the hook, so the provider path
// (and its retry) is genuinely exercised.
const brandingMock = vi.fn()

vi.mock('../api/client', () => ({
  api: { branding: (...args: unknown[]) => brandingMock(...args) },
}))

// Import the real provider + hook (after the mock is installed).
import { BrandingProvider, useBranding } from '../hooks/useBranding'

// In production BrandingProvider runs on the app-wide QueryClient; tests supply
// their own so the ambient client the provider relies on is present. retry: 3
// mirrors the app-wide client's default and pins the retry-recovery case below.
function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: 3, retryDelay: attempt => Math.min(250 * 2 ** attempt, 2000) } },
  })
  return (
    <QueryClientProvider client={client}>
      <BrandingProvider>{children}</BrandingProvider>
    </QueryClientProvider>
  )
}

describe('useBranding — directLocal mapping', () => {
  beforeEach(() => {
    // Each wrapper mount builds its own test client, so cases stay cache
    // isolated; only the shared api mock needs resetting between them.
    vi.clearAllMocks()
  })

  it('maps direct_local: true from the branding response to directLocal === true', async () => {
    brandingMock.mockResolvedValue({ bot_name: 'Bot', avatar: '/a.png', direct_local: true })
    const { result } = renderHook(() => useBranding(), { wrapper })

    await waitFor(() => expect(result.current.directLocal).toBe(true))
    expect(result.current.botName).toBe('Bot')
    expect(result.current.avatar).toBe('/a.png')
  })

  it('defaults directLocal to false when the response omits direct_local', async () => {
    brandingMock.mockResolvedValue({ bot_name: 'Remote', avatar: '/b.png' })
    const { result } = renderHook(() => useBranding(), { wrapper })

    await waitFor(() => expect(result.current.botName).toBe('Remote'))
    expect(result.current.directLocal).toBe(false)
  })

  it('holds the defaults (directLocal false) while every fetch fails', async () => {
    brandingMock.mockRejectedValue(new Error('network'))
    const { result } = renderHook(() => useBranding(), { wrapper })

    // Nothing resolved yet, so the provider keeps its defaults.
    expect(result.current.directLocal).toBe(false)
    expect(result.current.botName).toBe('Kiro Crew')
  })

  it('recovers directLocal true when a failed first fetch is followed by a successful retry', async () => {
    // First attempt rejects, the retry resolves with direct_local true. The
    // one-shot useEffect fetch this replaced would have stayed false forever.
    brandingMock
      .mockRejectedValueOnce(new Error('startup blip'))
      .mockResolvedValue({ bot_name: 'Local', avatar: '/c.png', direct_local: true })
    const { result } = renderHook(() => useBranding(), { wrapper })

    await waitFor(() => expect(result.current.directLocal).toBe(true), { timeout: 5000 })
    expect(brandingMock.mock.calls.length).toBeGreaterThan(1)
  })
})
