import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import ArtifactDeployPage from '../pages/ArtifactDeployPage'

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

describe('ArtifactDeployPage deployment listing that could not run', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('shows the reason a 200 list reply carries instead of the empty state', async () => {
    // A native Windows gateway answers the listing with HTTP 200 and its reason
    // in `error`, so the query succeeds and only the body says it could not run.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: RequestInfo | URL) => {
      const payload = String(input).endsWith('/api/deploy/list')
        ? { sites: [], configured: false, error: 'deploy features are not supported on Windows' }
        : {}
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    })

    render(<ArtifactDeployPage />, { wrapper })

    expect(await screen.findByText('deploy features are not supported on Windows')).toBeInTheDocument()
    expect(screen.queryByText(/No deployments yet/)).toBeNull()
    expect(screen.queryByText(/read live from AWS resource tags/)).toBeNull()
  })
})
