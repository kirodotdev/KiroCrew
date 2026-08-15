import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { useCloudDeploymentEnabled } from '../hooks/useCloudDeploymentEnabled'

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

function Probe() {
  const enabled = useCloudDeploymentEnabled()
  return <div>{enabled ? 'cloud-enabled' : 'cloud-disabled'}</div>
}

function mockProviders(providers: unknown) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input)
    const body = url.includes('/api/publish-providers') ? { providers } : {}
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  })
}

describe('useCloudDeploymentEnabled', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('is enabled when the core advertises its AWS destination', async () => {
    mockProviders([{ id: 'deploy-web-aws', endpoint: '/api/deploy/deploy' }, { id: 'artifactory' }])
    render(<Probe />, { wrapper })
    await waitFor(() => expect(screen.getByText('cloud-enabled')).toBeDefined())
  })

  it('is disabled when only internal destinations are advertised', async () => {
    // The internal-edition shape: Artifactory only, no AWS row.
    mockProviders([{ id: 'artifactory' }])
    render(<Probe />, { wrapper })
    await waitFor(() => expect(screen.getByText('cloud-disabled')).toBeDefined())
  })

  it('defaults to enabled before the list arrives', () => {
    // Hiding a working affordance because a list has not loaded yet is worse
    // than briefly showing one; the backend refuses the action regardless.
    mockProviders([])
    render(<Probe />, { wrapper })
    expect(screen.getByText('cloud-enabled')).toBeDefined()
  })
})
