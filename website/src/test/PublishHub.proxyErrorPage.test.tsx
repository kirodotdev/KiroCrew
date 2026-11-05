import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { PREVIEW_ARTIFACT_DEPLOY } from '../utils/previewFlags'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { PublishHub } from '../components/PublishHub'
import type { Artifact } from '../types'

/**
 * A proxy in front of the gateway can answer the publish confirm with its own
 * error page, which is not JSON. The banner must show the status, never the
 * JSON parse error that reading that page as JSON throws.
 */

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

const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })

beforeEach(() => { localStorage.setItem(PREVIEW_ARTIFACT_DEPLOY, '1') })
afterEach(() => vi.restoreAllMocks())

describe('PublishHub confirm answered by a proxy error page', () => {
  it('shows the status of an HTML 502, not a JSON parse error', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    // GET publish-providers
    fetchSpy.mockImplementationOnce(async () => json({
      providers: [{
        id: 'deploy-web',
        label: 'Public Web',
        icon: 'Globe',
        kinds: [],
        configured: true,
        setupRoute: '/deploy',
        endpoint: '/api/deploy/deploy',
      }],
    }))

    render(<PublishHub artifact={fakeArtifact} />, { wrapper })
    await waitFor(() => {
      expect(screen.getByText('Public Web')).toBeDefined()
    })
    fireEvent.click(screen.getByText('Public Web'))

    // Preview
    fetchSpy.mockImplementationOnce(async () => json({
      requires_confirm: true, message: 'Ready to publish', bytes: 1024, content_digest: 'abc123',
    }))
    const publishBtn = screen.getAllByRole('button')
      .find(b => b.textContent?.includes('Publish') && !b.textContent?.includes('Close'))
    fireEvent.click(publishBtn!)
    await waitFor(() => {
      expect(screen.getByText(/Ready to publish/)).toBeDefined()
    })

    // Confirm, answered by the proxy's own HTML page
    fetchSpy.mockImplementationOnce(async () =>
      new Response('<!DOCTYPE html><html><body><h1>502 Bad Gateway</h1></body></html>', {
        status: 502, headers: { 'Content-Type': 'text/html' },
      }))
    fireEvent.click(screen.getByText('Confirm & Publish'))
    fireEvent.click(
      await screen.findByRole('button', { name: 'I understand, publish publicly' }),
    )

    expect(await screen.findByText('HTTP 502')).toBeDefined()
    expect(screen.queryByText(/JSON|Unexpected token/)).toBeNull()
  })
})
