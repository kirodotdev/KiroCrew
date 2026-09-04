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

describe('PublishHub stale_preview digest flow (F4 R15)', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })

  it('sends expected_content_digest on confirm and handles stale_preview 409', async () => {
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

    // Mock preview call → requires_confirm with content_digest
    fetchSpy.mockImplementationOnce(async () =>
      new Response(JSON.stringify({
        requires_confirm: true,
        message: 'Ready to publish',
        bytes: 1024,
        content_digest: 'abc123deadbeef',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    )

    const publishBtns = screen.getAllByRole('button')
    const publishBtn = publishBtns.find(b => b.textContent?.includes('Publish') && !b.textContent?.includes('Close'))
    fireEvent.click(publishBtn!)

    await waitFor(() => {
      expect(screen.getByText(/Ready to publish/)).toBeDefined()
    })

    // Mock confirm call → 409 stale_preview
    fetchSpy.mockImplementationOnce(async (_url, init) => {
      const body = JSON.parse((init as RequestInit).body as string)
      expect(body.expected_content_digest).toBe('abc123deadbeef')
      expect(body.confirm).toBe(true)
      return new Response(JSON.stringify({
        error: 'content changed since preview',
        code: 'stale_preview',
      }), { status: 409, headers: { 'Content-Type': 'application/json' } })
    })

    fireEvent.click(screen.getByText('Confirm & Publish'))
    // #3599: the commit now happens through the blocking acknowledgment.
    fireEvent.click(
      await screen.findByRole('button', { name: 'I understand, publish publicly' }),
    )

    await waitFor(() => {
      expect(screen.getByText(/Content changed since preview/)).toBeDefined()
    })
  })

  it('sends expected_content_digest when present from preview', async () => {
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

    // Preview with digest
    fetchSpy.mockImplementationOnce(async () =>
      new Response(JSON.stringify({
        requires_confirm: true,
        message: 'Ready',
        bytes: 512,
        content_digest: 'sha256-valid',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    )

    const publishBtns = screen.getAllByRole('button')
    const publishBtn = publishBtns.find(b => b.textContent?.includes('Publish') && !b.textContent?.includes('Close'))
    fireEvent.click(publishBtn!)

    await waitFor(() => {
      expect(screen.getByText(/Ready/)).toBeDefined()
    })

    // Confirm → success — verify digest in payload
    fetchSpy.mockImplementationOnce(async (_url, init) => {
      const body = JSON.parse((init as RequestInit).body as string)
      expect(body.expected_content_digest).toBe('sha256-valid')
      return new Response(JSON.stringify({ url: 'https://example.com/app' }),
        { status: 200, headers: { 'Content-Type': 'application/json' } })
    })

    fireEvent.click(screen.getByText('Confirm & Publish'))
    // #3599: the commit now happens through the blocking acknowledgment.
    fireEvent.click(
      await screen.findByRole('button', { name: 'I understand, publish publicly' }),
    )

    await waitFor(() => {
      expect(screen.getByText('Published!')).toBeDefined()
    })
  })
})
