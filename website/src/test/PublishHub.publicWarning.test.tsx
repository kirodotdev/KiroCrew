import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { PublishHub } from '../components/PublishHub'
import type { Artifact } from '../types'

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

const fakeArtifact: Artifact = {
  slug: 'test-app',
  name: 'Test App',
  kind: 'widget',
  description: '',
  content: '',
  version: 1,
  created_at: '',
  updated_at: '',
  tags: [],
}

describe('PublishHub public-exposure warning (#3493)', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })

  it('shows the public/no-auth warning on the Confirm & Publish step', async () => {
    // GET publish-providers
    fetchSpy.mockImplementationOnce(async () =>
      new Response(JSON.stringify({
        providers: [{
          id: 'deploy-web',
          label: 'Public Web',
          icon: 'Globe',
          kinds: [],
          configured: true,
          setupRoute: '/deploy',
          endpoint: '/api/deploy/deploy',
        }],
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    )

    render(<PublishHub artifact={fakeArtifact} />, { wrapper })

    await waitFor(() => {
      expect(screen.getByText('Public Web')).toBeDefined()
    })
    fireEvent.click(screen.getByText('Public Web'))

    // The warning must NOT render before the preview/confirm step exists.
    expect(screen.queryByText(/Anyone with the published link can view this content/)).toBeNull()

    // Mock preview call -> requires_confirm
    fetchSpy.mockImplementationOnce(async () =>
      new Response(JSON.stringify({
        requires_confirm: true,
        message: 'Ready to publish',
        bytes: 1024,
        content_digest: 'abc123',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    )

    const publishBtns = screen.getAllByRole('button')
    const publishBtn = publishBtns.find(b => b.textContent?.includes('Publish') && !b.textContent?.includes('Close'))
    fireEvent.click(publishBtn!)

    // The confirm step must carry the exposure warning next to the confirm button.
    await waitFor(() => {
      expect(screen.getByText(/Anyone with the published link can view this content\. It is served on the public internet with no authentication\./)).toBeDefined()
    })
    expect(screen.getByText(/Confirm & Publish/)).toBeDefined()
  })

  it('shows the public/no-auth warning on the scan-blocked override path', async () => {
    // GET publish-providers
    fetchSpy.mockImplementationOnce(async () =>
      new Response(JSON.stringify({
        providers: [{
          id: 'deploy-web',
          label: 'Public Web',
          icon: 'Globe',
          kinds: [],
          configured: true,
          setupRoute: '/deploy',
          endpoint: '/api/deploy/deploy',
        }],
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    )

    render(<PublishHub artifact={fakeArtifact} />, { wrapper })

    await waitFor(() => {
      expect(screen.getByText('Public Web')).toBeDefined()
    })
    fireEvent.click(screen.getByText('Public Web'))

    // Mock preview call -> non-credential scan block (overridable)
    fetchSpy.mockImplementationOnce(async () =>
      new Response(JSON.stringify({
        blocked: true,
        reason: 'scan',
        findings: 'aws_key at index.html:12',
        count: 1,
        credential: false,
        content_digest: 'abc123',
      }), { status: 409, headers: { 'Content-Type': 'application/json' } })
    )

    const publishBtns = screen.getAllByRole('button')
    const publishBtn = publishBtns.find(b => b.textContent?.includes('Publish') && !b.textContent?.includes('Close'))
    fireEvent.click(publishBtn!)

    // The override branch commits a publish too, so it must carry the same
    // exposure warning next to the "Override & Publish Anyway" control.
    await waitFor(() => {
      expect(screen.getByText(/Override & Publish Anyway/)).toBeDefined()
    })
    expect(screen.getByText(/Anyone with the published link can view this content\. It is served on the public internet with no authentication\./)).toBeDefined()
  })
})
