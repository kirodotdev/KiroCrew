import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
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

/** Respond to /api/deploy/config with `body`, and {} to everything else. */
function mockConfig(body: unknown) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input)
    const payload = url.includes('/api/deploy/config') ? body : {}
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  })
}

describe('ArtifactDeployPage cloud-deployment gating', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('hides the deploy console and explains why when the platform withholds it', async () => {
    mockConfig({ cloudDeploymentEnabled: false })

    render(<ArtifactDeployPage />, { wrapper })

    await waitFor(() => {
      expect(screen.getByText(/Cloud deployment is disabled/)).toBeDefined()
    })
    // The PROVISIONING half is gone — no getting-started guide.
    expect(screen.queryByText(/Published URLs are public-by-link/)).toBeNull()
  })

  it('keeps the deployments table reachable when deployment is withheld', async () => {
    // A policy that stops NEW deployments must not strand existing exposure: the
    // recall/destroy path has to stay usable, which is the same reason those
    // routes are ungated on the backend.
    mockConfig({ cloudDeploymentEnabled: false })

    render(<ArtifactDeployPage />, { wrapper })

    await waitFor(() => {
      expect(screen.getByText(/Cloud deployment is disabled/)).toBeDefined()
    })
    // "Deployments" appears in more than one node (heading + copy), so assert
    // presence rather than uniqueness.
    expect(screen.getAllByText(/Deployments/).length).toBeGreaterThan(0)
  })

  it('renders the console when the platform permits deployment', async () => {
    mockConfig({ cloudDeploymentEnabled: true })

    render(<ArtifactDeployPage />, { wrapper })

    await waitFor(() => {
      expect(screen.getByText(/Published URLs are public-by-link/)).toBeDefined()
    })
    expect(screen.queryByText(/Cloud deployment is disabled/)).toBeNull()
  })

  it('renders the console when the flag is absent (older backend)', async () => {
    // Version skew must not hide a working deploy surface: only an explicit
    // false withholds it.
    mockConfig({})

    render(<ArtifactDeployPage />, { wrapper })

    await waitFor(() => {
      expect(screen.getByText(/Published URLs are public-by-link/)).toBeDefined()
    })
    expect(screen.queryByText(/Cloud deployment is disabled/)).toBeNull()
  })
})
