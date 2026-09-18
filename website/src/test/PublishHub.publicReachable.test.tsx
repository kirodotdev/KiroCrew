import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { PublishHub, buildProviderList } from '../components/PublishHub'
import type { Artifact, PublishProviderDescriptor } from '../types'

/**
 * The public-exposure warning and the blocking acknowledgment both say the content is
 * going onto the open internet. A destination that stores it privately behind a login
 * declares `public_reachable: false`, and then NEITHER surface may appear: a gate that
 * lies where the destination is private teaches the user to click past it where it is
 * public. Everything else (omitted, `true`) keeps both, exactly as before.
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

const WARNING = /Anyone with the published link can view this content/

const coreDescriptor = (over: Record<string, unknown> = {}) => ({
  name: 'default',
  display_name: 'Team drive',
  capabilities: ['sharing'],
  kind_support: 'native',
  capable: true,
  available: true,
  sharing_model: {
    supports_private: true,
    supports_shared: false,
    supports_public: true,
    principal_kind: 'none',
    supports_roles: false,
    supports_expiration: false,
    programmable: false,
  },
  sync_model: { authority: 'local', concurrency: 'token', collab_mode: 'mirror' },
  discovery_model: {
    list_mine: false,
    list_shared_with_me: false,
    list_public: false,
    full_text_search: false,
    pull_by_id: false,
  },
  ...over,
})

/** App registry empty, core registry carrying the given rows; every publish POST succeeds. */
function mockRegistries(fetchSpy: ReturnType<typeof vi.spyOn>, core: Record<string, unknown>[]) {
  fetchSpy.mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/artifacts/publish-providers')) {
      return new Response(JSON.stringify({ providers: core, kind: 'markdown' }), { status: 200 })
    }
    if (url.includes('/api/publish-providers')) {
      return new Response(JSON.stringify({ providers: [] }), { status: 200 })
    }
    return new Response(JSON.stringify({ publication: { view_url: 'https://drive/x' } }), { status: 200 })
  })
}

function publishPosts(fetchSpy: ReturnType<typeof vi.spyOn>) {
  return fetchSpy.mock.calls.filter(
    c => String(c[0]).includes('/publish') && (c[1] as RequestInit | undefined)?.method === 'POST',
  )
}

describe('buildProviderList carries public_reachable', () => {
  it('only an explicit false turns the gate off; omitted and true keep it on', () => {
    const rows = buildProviderList([], 'markdown', [
      coreDescriptor({ name: 'omitted' }),
      coreDescriptor({ name: 'yes', public_reachable: true }),
      coreDescriptor({ name: 'no', public_reachable: false }),
    ] as unknown as PublishProviderDescriptor[])
    const byId = Object.fromEntries(rows.map(r => [r.id, r.publicReachable]))
    expect(byId).toEqual({ omitted: true, yes: true, no: false })
  })

  it('an app row is the public-web deploy surface and is always reachable', () => {
    const rows = buildProviderList(
      [{ id: 'deploy-web', label: 'Public Web', icon: 'Globe', kinds: [], configured: true, setupRoute: '/deploy', endpoint: '/api/deploy/deploy' }],
      'markdown',
    )
    expect(rows).toHaveLength(1)
    expect(rows[0].publicReachable).toBe(true)
  })
})

describe('PublishHub gates the exposure warning and acknowledgment on public_reachable', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    // A fresh spy per test: the recorded calls are what the assertions below count,
    // and a spy carried over from the previous test would carry its publish POST too.
    vi.restoreAllMocks()
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })

  it('a destination that requires authentication shows no warning and publishes on confirm', async () => {
    mockRegistries(fetchSpy, [coreDescriptor({ public_reachable: false })])
    render(<PublishHub artifact={fakeArtifact} />, { wrapper })
    fireEvent.click(await screen.findByText('Team drive'))
    fireEvent.click(await screen.findByRole('button', { name: /publish/i }))

    // Confirm step is reached, with no exposure banner on it.
    const confirm = await screen.findByRole('button', { name: /confirm/i })
    expect(screen.queryByText(WARNING)).toBeNull()
    expect(publishPosts(fetchSpy)).toHaveLength(0)

    fireEvent.click(confirm)

    // No acknowledgment dialog opens; the confirm click itself publishes.
    await waitFor(() => expect(publishPosts(fetchSpy)).toHaveLength(1))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('a destination that omits the field still shows the warning and asks first', async () => {
    mockRegistries(fetchSpy, [coreDescriptor()])
    render(<PublishHub artifact={fakeArtifact} />, { wrapper })
    fireEvent.click(await screen.findByText('Team drive'))
    fireEvent.click(await screen.findByRole('button', { name: /publish/i }))

    const confirm = await screen.findByRole('button', { name: /confirm/i })
    expect(screen.getByText(WARNING)).toBeTruthy()

    fireEvent.click(confirm)

    // The blocking acknowledgment opens and nothing has been posted yet.
    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(publishPosts(fetchSpy)).toHaveLength(0)
  })

  it('a destination that declares true behaves exactly like one that omits the field', async () => {
    mockRegistries(fetchSpy, [coreDescriptor({ public_reachable: true })])
    render(<PublishHub artifact={fakeArtifact} />, { wrapper })
    fireEvent.click(await screen.findByText('Team drive'))
    fireEvent.click(await screen.findByRole('button', { name: /publish/i }))

    const confirm = await screen.findByRole('button', { name: /confirm/i })
    expect(screen.getByText(WARNING)).toBeTruthy()
    fireEvent.click(confirm)
    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(publishPosts(fetchSpy)).toHaveLength(0)
  })
})
