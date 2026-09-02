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

describe('PublishHub 409 scan-blocked flow', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })

  it('renders scan findings from 409 response and sends override_scan on explicit override', async () => {
    // First call: GET publish-providers
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

    // Mock the first publish call → 409 scan-blocked
    fetchSpy.mockImplementationOnce(async () =>
      new Response(JSON.stringify({
        blocked: true,
        reason: 'scan',
        findings: 'AKIA1234567890123456 found in public/config.js',
        count: 1,
      }), { status: 409, headers: { 'Content-Type': 'application/json' } })
    )

    // Click Publish button (there's also a "Publish" header — target the button)
    const publishBtns = screen.getAllByRole('button')
    const publishBtn = publishBtns.find(b => b.textContent?.includes('Publish') && !b.textContent?.includes('Close'))
    expect(publishBtn).toBeDefined()
    fireEvent.click(publishBtn!)

    await waitFor(() => {
      expect(screen.getByText(/Scan blocked/)).toBeDefined()
      expect(screen.getByText(/AKIA1234567890123456/)).toBeDefined()
    })

    // Mock the override call → success
    fetchSpy.mockImplementationOnce(async (_url, init) => {
      const body = JSON.parse((init as RequestInit).body as string)
      expect(body.override_scan).toBe(true)
      expect(body.confirm).toBe(true)
      return new Response(JSON.stringify({
        url: 'https://d123.cloudfront.net/test-app/',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    })

    fireEvent.click(screen.getByText('Override & Publish Anyway'))
    // #3599: the commit now happens through the blocking acknowledgment.
    fireEvent.click(
      await screen.findByRole('button', { name: 'I understand, publish publicly' }),
    )

    await waitFor(() => {
      expect(screen.getByText('Published!')).toBeDefined()
    })
  })

  it('credential 409 hides override button and shows explanatory text', async () => {
    // First call: GET publish-providers
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

    // Mock the publish call → 409 with credential=true
    fetchSpy.mockImplementationOnce(async () =>
      new Response(JSON.stringify({
        blocked: true,
        reason: 'scan',
        findings: 'AWS secret key found in deploy.js',
        count: 1,
        credential: true,
      }), { status: 409, headers: { 'Content-Type': 'application/json' } })
    )

    const publishBtns = screen.getAllByRole('button')
    const publishBtn = publishBtns.find(b => b.textContent?.includes('Publish') && !b.textContent?.includes('Close'))
    fireEvent.click(publishBtn!)

    await waitFor(() => {
      expect(screen.getByText(/Scan blocked/)).toBeDefined()
      // Override button should NOT be present
      expect(screen.queryByText('Override & Publish Anyway')).toBeNull()
      // Explanatory credential text shown instead
      expect(screen.getByText(/cannot be overridden/)).toBeDefined()
    })
  })

  it('info-class 409 shows override button', async () => {
    // First call: GET publish-providers
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

    // Mock the publish call → 409 without credential flag (info-class)
    fetchSpy.mockImplementationOnce(async () =>
      new Response(JSON.stringify({
        blocked: true,
        reason: 'scan',
        findings: 'Internal hostname found in config.js',
        count: 1,
      }), { status: 409, headers: { 'Content-Type': 'application/json' } })
    )

    const publishBtns = screen.getAllByRole('button')
    const publishBtn = publishBtns.find(b => b.textContent?.includes('Publish') && !b.textContent?.includes('Close'))
    fireEvent.click(publishBtn!)

    await waitFor(() => {
      expect(screen.getByText(/Scan blocked/)).toBeDefined()
      // Override button SHOULD be present for info-class findings
      expect(screen.getByText('Override & Publish Anyway')).toBeDefined()
    })
  })
})


describe('PublishHub ttl_hours payload (F3 R11)', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })

  it('sends ttl_hours: 0 (persistent) by default in the confirm payload', async () => {
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

    // Mock preview call → requires_confirm
    fetchSpy.mockImplementationOnce(async () =>
      new Response(JSON.stringify({
        requires_confirm: true,
        message: 'Ready to publish',
        bytes: 1024,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    )

    const publishBtns = screen.getAllByRole('button')
    const publishBtn = publishBtns.find(b => b.textContent?.includes('Publish') && !b.textContent?.includes('Close'))
    fireEvent.click(publishBtn!)

    await waitFor(() => {
      expect(screen.getByText(/Ready to publish/)).toBeDefined()
    })

    // Mock confirm call — verify ttl_hours=0 in the payload
    fetchSpy.mockImplementationOnce(async (_url, init) => {
      const body = JSON.parse((init as RequestInit).body as string)
      expect(body.ttl_hours).toBe(0)
      expect(body.confirm).toBe(true)
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
