import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { PublishHub, effectiveExpiry, expiryOptionsFor } from '../components/PublishHub'
import type { Artifact } from '../types'

/**
 * A built-in destination that declares its own expiry periods offers them in the panel
 * on a first publish, and the publish request carries the chosen period as
 * `expires_in_days`. A destination that declares none gets no control and the request
 * leaves the key out, so it is exactly the request the panel sent before.
 *
 * Picking a period is asserted through the pure helpers: `SimpleSelect` renders a Radix
 * trigger off the touch path, so a `fireEvent` against it cannot drive the choice (see
 * PublishHub.ttlLeak.test.tsx). The render tests cover what the panel sends and shows.
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
  slug: 'test-doc',
  name: 'Test Doc',
  kind: 'markdown',
  description: '',
  content: '',
  version: 1,
  created_at: '',
  updated_at: '',
  tags: [],
}

const EXPIRING = {
  supports_expiration: true,
  expiration_choices_days: [30, 90, 365, 730],
  allows_no_expiration: false,
  default_expiration_days: 30,
}

const coreDescriptor = (sharing: Record<string, unknown> = {}, publicReachable = false) => ({
  name: 'paste',
  display_name: 'Paste service',
  capabilities: ['sharing'],
  kind_support: 'native',
  capable: true,
  available: true,
  public_reachable: publicReachable,
  sharing_model: {
    supports_private: true,
    supports_shared: false,
    supports_public: true,
    principal_kind: 'none',
    supports_roles: false,
    supports_expiration: false,
    programmable: true,
    out_of_band_url: '',
    ...sharing,
  },
  sync_model: { authority: 'kirocrew', concurrency: 'lww', collab_mode: 'mirror' },
  discovery_model: {
    list_mine: true,
    list_shared_with_me: false,
    list_public: false,
    full_text_search: false,
    pull_by_id: true,
  },
})

function mockRegistries(fetchSpy: ReturnType<typeof vi.spyOn>, core: Record<string, unknown>[]) {
  fetchSpy.mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/artifacts/publish-providers')) {
      return new Response(JSON.stringify({ providers: core, kind: 'markdown' }), { status: 200 })
    }
    if (url.includes('/api/publish-providers')) {
      return new Response(JSON.stringify({ providers: [] }), { status: 200 })
    }
    return new Response(JSON.stringify({ publication: { view_url: 'https://paste/x' } }), { status: 200 })
  })
}

function publishBodies(fetchSpy: ReturnType<typeof vi.spyOn>): Record<string, unknown>[] {
  return fetchSpy.mock.calls
    .filter(c => String(c[0]).includes('/publish') && (c[1] as RequestInit | undefined)?.method === 'POST')
    .map(c => JSON.parse(String((c[1] as RequestInit).body)))
}

const row = (sharing: Record<string, unknown>) =>
  ({
    id: 'paste',
    label: 'Paste service',
    configured: true,
    core: coreDescriptor(sharing),
  }) as unknown as Parameters<typeof expiryOptionsFor>[0]

describe('expiryOptionsFor / effectiveExpiry', () => {
  it('offers the declared periods on a first publish', () => {
    expect(expiryOptionsFor(row(EXPIRING), false)).toEqual([30, 90, 365, 730])
  })

  it('adds "no expiry" only when the destination allows it', () => {
    expect(expiryOptionsFor(row({ ...EXPIRING, allows_no_expiration: true }), false)).toEqual([
      30, 90, 365, 730, null,
    ])
  })

  it('offers nothing without declared periods, from an older gateway, or on a re-publish', () => {
    expect(expiryOptionsFor(row({ supports_expiration: true }), false)).toEqual([])
    expect(expiryOptionsFor(row(EXPIRING), true)).toEqual([])
    expect(expiryOptionsFor(undefined, false)).toEqual([])
  })

  it('carries the pick, or the declared default when nothing usable was picked', () => {
    expect(effectiveExpiry(730, row(EXPIRING), false)).toBe(730)
    expect(effectiveExpiry(undefined, row(EXPIRING), false)).toBe(30)
    // A period picked on another row does not ride along onto this one.
    expect(effectiveExpiry(7, row(EXPIRING), false)).toBe(30)
    expect(effectiveExpiry(null, row(EXPIRING), false)).toBe(30)
    const openEnded = row({ ...EXPIRING, allows_no_expiration: true, default_expiration_days: null })
    expect(effectiveExpiry(undefined, openEnded, false)).toBeNull()
  })

  it('leaves the key out where no choice is offered', () => {
    expect(effectiveExpiry(730, row({}), false)).toBeUndefined()
    expect(effectiveExpiry(730, row(EXPIRING), true)).toBeUndefined()
  })
})

describe('PublishHub expiry choice for a built-in destination', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    vi.restoreAllMocks()
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })

  it('shows the control, repeats the default on the confirm step and sends it', async () => {
    mockRegistries(fetchSpy, [coreDescriptor(EXPIRING)])
    render(<PublishHub artifact={fakeArtifact} />, { wrapper })
    fireEvent.click(await screen.findByText('Paste service'))

    expect(await screen.findByLabelText(/^expires$/i)).toBeTruthy()
    expect(screen.queryByLabelText(/time to live/i)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /^publish$/i }))

    const confirm = await screen.findByRole('button', { name: /confirm/i })
    expect(screen.getByText(/after 30 days/i)).toBeTruthy()
    fireEvent.click(confirm)

    await waitFor(() => expect(publishBodies(fetchSpy)).toHaveLength(1))
    expect(publishBodies(fetchSpy)[0].expires_in_days).toBe(30)
  })

  it('shows no control and sends no expiry when the destination declares none', async () => {
    mockRegistries(fetchSpy, [coreDescriptor({ supports_expiration: true })])
    render(<PublishHub artifact={fakeArtifact} />, { wrapper })
    fireEvent.click(await screen.findByText('Paste service'))
    await screen.findByRole('radio', { name: /private/i })
    expect(screen.queryByLabelText(/^expires$/i)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /^publish$/i }))
    fireEvent.click(await screen.findByRole('button', { name: /confirm/i }))

    await waitFor(() => expect(publishBodies(fetchSpy)).toHaveLength(1))
    expect(publishBodies(fetchSpy)[0]).not.toHaveProperty('expires_in_days')
  })

  it('offers no expiry on a re-publish and sends none', async () => {
    mockRegistries(fetchSpy, [coreDescriptor(EXPIRING)])
    const published = {
      ...fakeArtifact,
      publication: { visibility: 'PRIVATE', view_url: 'https://paste/x' },
    } as unknown as Artifact
    render(<PublishHub artifact={published} />, { wrapper })
    fireEvent.click(await screen.findByText('Paste service'))
    fireEvent.click(await screen.findByRole('button', { name: /^publish$/i }))
    expect(screen.queryByLabelText(/^expires$/i)).toBeNull()

    fireEvent.click(await screen.findByRole('button', { name: /confirm|save privately/i }))
    await waitFor(() => expect(publishBodies(fetchSpy)).toHaveLength(1))
    expect(publishBodies(fetchSpy)[0]).not.toHaveProperty('expires_in_days')
  })

  it('names the period in the public-exposure acknowledgment', async () => {
    mockRegistries(fetchSpy, [coreDescriptor(EXPIRING, true)])
    render(<PublishHub artifact={fakeArtifact} />, { wrapper })
    fireEvent.click(await screen.findByText('Paste service'))
    fireEvent.click(await screen.findByRole('button', { name: /^publish$/i }))
    fireEvent.click(await screen.findByRole('button', { name: /confirm/i }))

    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(screen.getByText(/stays public for 30 days, then the destination removes it/i)).toBeTruthy()
    expect(publishBodies(fetchSpy)).toHaveLength(0)
  })
})
