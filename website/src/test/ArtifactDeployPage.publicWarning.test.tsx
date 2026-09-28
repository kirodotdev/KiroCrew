import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
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

describe('ArtifactDeployPage public-by-link warning (#3493)', () => {
  beforeEach(() => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      new Response(JSON.stringify({}), { status: 200, headers: { 'Content-Type': 'application/json' } })
    )
  })

  it('shows the public-by-link warning while "How this is secured" is collapsed', async () => {
    render(<ArtifactDeployPage />, { wrapper })

    // The warning is always visible, even though the security section
    // defaults to collapsed.
    await waitFor(() => {
      expect(screen.getByText(/Published URLs are public-by-link: anyone with the link can view the content/)).toBeDefined()
    })

    // The collapsed section's body is NOT rendered — proving the warning
    // does not depend on the user expanding it.
    expect(screen.queryByText(/The origin bucket is private/)).toBeNull()
  })

  it('keeps the random *.cloudfront.net domain fact in the expanded security section', async () => {
    render(<ArtifactDeployPage />, { wrapper })

    // Hoisting the exposure claim into the always-visible banner must not
    // silently drop the OTHER facts the old bullet carried (#3538 review).
    const toggle = await screen.findByText(/How this is secured/)
    fireEvent.click(toggle)

    await waitFor(() => {
      expect(screen.getByText(/Content is served at a random \*\.cloudfront\.net domain/)).toBeDefined()
    })
    expect(screen.getByText(/don't publish anything you wouldn't put on the open internet/)).toBeDefined()
  })

  it('shows the warning on the Pending confirmations card, next to the confirm button', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/pending')) {
        return new Response(JSON.stringify({
          pending: [{
            id: 'p1',
            site_id: 'my-app',
            artifact_slug: 'my-app',
            local_dir: '',
            profile: 'default',
            ttl_hours: 72,
            scan_summary: 'clean',
            content_digest: 'abc',
            created_at_epoch: Date.now() / 1000,
            override_scan_required: false,
          }],
        }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      }
      return new Response(JSON.stringify({}), { status: 200, headers: { 'Content-Type': 'application/json' } })
    })

    render(<ArtifactDeployPage />, { wrapper })

    // Each pending entry carries the exposure warning beside its own confirm
    // button — a card-level banner scrolls away when many entries stack up.
    await waitFor(() => {
      expect(screen.getByText('Confirm Deploy')).toBeDefined()
    })
    expect(screen.getByText(/Anyone with the published link can view this content\. It is served on the public internet with no authentication\./)).toBeDefined()
  })
})
