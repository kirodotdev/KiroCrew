import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// First run's chapters write the host's flags, so a failed first-run state read
// must be told apart from one that answered: a browser with no flags of its own
// would otherwise replay first run.
const themeBootFn = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    themes: () => Promise.resolve({ themes: [] }),
    themeDetail: () => Promise.resolve({}),
    themeBoot: () => themeBootFn(),
    updateThemeConfig: () => Promise.resolve({}),
  },
}))

import { ThemeProvider, useTheme } from '../hooks/useTheme'

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  )
}

describe('useTheme: the first-run state read', () => {
  beforeEach(() => { localStorage.clear(); themeBootFn.mockReset() })
  afterEach(() => { localStorage.clear() })

  it('settles without being read when the read fails', async () => {
    themeBootFn.mockRejectedValue(new Error('boot read blocked'))
    const { result } = renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(result.current.themeBootReady).toBe(true))
    expect(result.current.themeBootRead).toBe(false)
  })

  it('settles as read when the read answers', async () => {
    themeBootFn.mockResolvedValue({ onboarded: true, import_onboarded: true, privacy_acked: true })
    const { result } = renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(result.current.themeBootReady).toBe(true))
    expect(result.current.themeBootRead).toBe(true)
  })
})
