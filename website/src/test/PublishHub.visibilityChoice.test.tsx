import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { PublishHub, effectiveVisibility, visibilityOptionsFor } from '../components/PublishHub'
import type { Artifact } from '../types'
import { PREVIEW_ARTIFACT_DEPLOY } from '../utils/previewFlags'

/**
 * A built-in destination whose sharing model supports both private and public
 * publications offers that choice in the panel, and the publish request carries the
 * chosen visibility. Public stays the default, so a user who does not touch the
 * control gets exactly the request they got before. Choosing private sends PRIVATE
 * and drops the public-exposure warning and acknowledgment, which describe a public
 * link and would be false for a private one.
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

const coreDescriptor = (sharing: Record<string, unknown> = {}) => ({
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
    ...sharing,
  },
  sync_model: { authority: 'local', concurrency: 'token', collab_mode: 'mirror' },
  discovery_model: {
    list_mine: false,
    list_shared_with_me: false,
    list_public: false,
    full_text_search: false,
    pull_by_id: false,
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
    return new Response(JSON.stringify({ publication: { view_url: 'https://drive/x' } }), { status: 200 })
  })
}

function publishBodies(fetchSpy: ReturnType<typeof vi.spyOn>): Record<string, unknown>[] {
  return fetchSpy.mock.calls
    .filter(c => String(c[0]).includes('/publish') && (c[1] as RequestInit | undefined)?.method === 'POST')
    .map(c => JSON.parse(String((c[1] as RequestInit).body)))
}

const row = (sharing: Record<string, unknown>) =>
  ({
    id: 'default',
    label: 'Team drive',
    configured: true,
    core: coreDescriptor(sharing),
  }) as unknown as Parameters<typeof visibilityOptionsFor>[0]

describe('visibilityOptionsFor / effectiveVisibility', () => {
  it('offers private and public when the destination supports both', () => {
    expect(visibilityOptionsFor(row({}), false)).toEqual(['PRIVATE', 'PUBLIC'])
  })

  it('offers no choice when the destination supports only one', () => {
    expect(visibilityOptionsFor(row({ supports_private: false }), false)).toEqual([])
    expect(visibilityOptionsFor(undefined, false)).toEqual([])
  })

  it('offers no choice for an artifact that is already published, and keeps its visibility', () => {
    expect(visibilityOptionsFor(row({}), true)).toEqual([])
    expect(effectiveVisibility('PUBLIC', row({}), 'PRIVATE')).toBe('PRIVATE')
    expect(effectiveVisibility('PUBLIC', row({}), 'SHARED')).toBe('SHARED')
    expect(effectiveVisibility('PRIVATE', row({}), 'PUBLIC')).toBe('PUBLIC')
  })

  it('a non-core row is always public, whatever the artifact record says', () => {
    const appRow = { id: 'deploy-web', label: 'Public web', configured: true } as unknown as Parameters<
      typeof effectiveVisibility
    >[1]
    expect(effectiveVisibility('PRIVATE', appRow, 'PRIVATE')).toBe('PUBLIC')
    expect(effectiveVisibility('PRIVATE', appRow, null)).toBe('PUBLIC')
  })

  it('a choice the selected row cannot honour falls back to public', () => {
    expect(effectiveVisibility('PRIVATE', row({}), null)).toBe('PRIVATE')
    expect(effectiveVisibility('PRIVATE', row({ supports_private: false }), null)).toBe('PUBLIC')
    expect(effectiveVisibility('PUBLIC', row({}), null)).toBe('PUBLIC')
  })
})

describe('PublishHub visibility choice for a built-in destination', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    vi.restoreAllMocks()
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })

  it('publishes PRIVATE with no exposure warning or acknowledgment when private is chosen', async () => {
    mockRegistries(fetchSpy, [coreDescriptor()])
    render(<PublishHub artifact={fakeArtifact} />, { wrapper })
    fireEvent.click(await screen.findByText('Team drive'))

    fireEvent.click(await screen.findByRole('radio', { name: /private/i }))
    fireEvent.click(screen.getByRole('button', { name: /^publish$/i }))

    const confirm = await screen.findByRole('button', { name: /confirm/i })
    expect(screen.queryByText(WARNING)).toBeNull()
    // The confirm step repeats the choice, so the user can check it before publishing.
    expect(screen.getByText(/only you/i)).toBeTruthy()
    fireEvent.click(confirm)

    await waitFor(() => expect(publishBodies(fetchSpy)).toHaveLength(1))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(publishBodies(fetchSpy)[0].visibility).toBe('PRIVATE')
  })

  it('keeps public as the default, with the warning and the acknowledgment', async () => {
    mockRegistries(fetchSpy, [coreDescriptor()])
    render(<PublishHub artifact={fakeArtifact} />, { wrapper })
    fireEvent.click(await screen.findByText('Team drive'))

    const publicRadio = await screen.findByRole('radio', { name: /public/i })
    expect((publicRadio as HTMLInputElement).checked).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: /^publish$/i }))

    const confirm = await screen.findByRole('button', { name: /confirm/i })
    expect(screen.getByText(WARNING)).toBeTruthy()
    fireEvent.click(confirm)
    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(publishBodies(fetchSpy)).toHaveLength(0)
  })

  it('shows no choice and keeps the public gate when the artifact is already published', async () => {
    mockRegistries(fetchSpy, [coreDescriptor()])
    const published = {
      ...fakeArtifact,
      publication: { visibility: 'PUBLIC', view_url: 'https://drive/x' },
    } as unknown as Artifact
    render(<PublishHub artifact={published} />, { wrapper })
    fireEvent.click(await screen.findByText('Team drive'))
    fireEvent.click(await screen.findByRole('button', { name: /^publish$/i }))
    expect(screen.queryByRole('radio')).toBeNull()

    const confirm = await screen.findByRole('button', { name: /confirm/i })
    expect(screen.getByText(WARNING)).toBeTruthy()
    fireEvent.click(confirm)
    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(publishBodies(fetchSpy)).toHaveLength(0)
  })

  it('re-publishes a private publication as PRIVATE, with no choice and no acknowledgment', async () => {
    mockRegistries(fetchSpy, [coreDescriptor()])
    const published = {
      ...fakeArtifact,
      publication: { visibility: 'PRIVATE', view_url: '' },
    } as unknown as Artifact
    render(<PublishHub artifact={published} />, { wrapper })
    fireEvent.click(await screen.findByText('Team drive'))
    fireEvent.click(await screen.findByRole('button', { name: /^publish$/i }))
    expect(screen.queryByRole('radio')).toBeNull()

    const confirm = await screen.findByRole('button', { name: /confirm/i })
    expect(screen.queryByText(WARNING)).toBeNull()
    // The confirm step names the visibility the re-publish keeps.
    expect(screen.getByText(/only you/i)).toBeTruthy()
    fireEvent.click(confirm)
    await waitFor(() => expect(publishBodies(fetchSpy)).toHaveLength(1))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(publishBodies(fetchSpy)[0].visibility).toBe('PRIVATE')
  })

  it('re-publishes a shared publication as SHARED with its people unchanged', async () => {
    mockRegistries(fetchSpy, [coreDescriptor({ supports_shared: true })])
    const published = {
      ...fakeArtifact,
      publication: { visibility: 'SHARED', shared_with: ['alice', 'bob'], view_url: '' },
    } as unknown as Artifact
    render(<PublishHub artifact={published} />, { wrapper })
    fireEvent.click(await screen.findByText('Team drive'))
    fireEvent.click(await screen.findByRole('button', { name: /^publish$/i }))
    expect(screen.queryByRole('radio')).toBeNull()

    fireEvent.click(await screen.findByRole('button', { name: /confirm/i }))
    await waitFor(() => expect(publishBodies(fetchSpy)).toHaveLength(1))
    expect(publishBodies(fetchSpy)[0]).toMatchObject({ visibility: 'SHARED', shared_with: ['alice', 'bob'] })
  })

  it('the public-web deploy row keeps the warning and acknowledgment for a privately published artifact', async () => {
    localStorage.setItem(PREVIEW_ARTIFACT_DEPLOY, '1')
    fetchSpy.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/api/artifacts/publish-providers')) {
        return new Response(JSON.stringify({ providers: [], kind: 'markdown' }), { status: 200 })
      }
      if (url.includes('/api/publish-providers')) {
        return new Response(JSON.stringify({
          providers: [{
            id: 'deploy-web', label: 'Public Web', icon: 'Globe', kinds: [], configured: true,
            setupRoute: '/deploy', endpoint: '/api/deploy/deploy',
          }],
        }), { status: 200 })
      }
      if (init?.method === 'POST') {
        return new Response(JSON.stringify({ requires_confirm: true, message: 'Ready', content_digest: 'd' }), { status: 200 })
      }
      return new Response('{}', { status: 200 })
    })
    const privatelyPublished = {
      ...fakeArtifact,
      publication: { visibility: 'PRIVATE', view_url: '' },
    } as unknown as Artifact
    render(<PublishHub artifact={privatelyPublished} />, { wrapper })
    fireEvent.click(await screen.findByText('Public Web'))
    fireEvent.click(await screen.findByRole('button', { name: /^publish$/i }))

    const confirm = await screen.findByRole('button', { name: /confirm/i })
    expect(screen.getByText(WARNING)).toBeTruthy()
    const before = fetchSpy.mock.calls.length
    fireEvent.click(confirm)
    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(fetchSpy.mock.calls.length).toBe(before)
  })

  it('shows no choice when the destination supports only public', async () => {
    mockRegistries(fetchSpy, [coreDescriptor({ supports_private: false })])
    render(<PublishHub artifact={fakeArtifact} />, { wrapper })
    fireEvent.click(await screen.findByText('Team drive'))
    await screen.findByRole('button', { name: /^publish$/i })
    expect(screen.queryByRole('radio')).toBeNull()
  })
})
