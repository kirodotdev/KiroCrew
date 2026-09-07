import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { PublishHub } from '../components/PublishHub'
import type { Artifact } from '../types'

/**
 * Behaviour, not shape. The sibling `PublishHub.coreProviders` file pins what
 * `buildProviderList` RETURNS; these pin what the panel DOES with a core row -- which is
 * where the real risk is: a core destination that published on the first click would make
 * content world-readable without the acknowledgment gate (#3599) ever opening.
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

const coreDescriptor = (over: Record<string, unknown> = {}) => ({
  name: 'default',
  display_name: 'Public web (shared drive)',
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

/** App registry empty, core registry carrying one row. */
function mockRegistries(fetchSpy: ReturnType<typeof vi.spyOn>, core: Record<string, unknown>[]) {
  fetchSpy.mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/artifacts/publish-providers')) {
      return new Response(JSON.stringify({ providers: core, kind: 'markdown' }), { status: 200 })
    }
    if (url.includes('/api/publish-providers')) {
      return new Response(JSON.stringify({ providers: [] }), { status: 200 })
    }
    // Any publish POST reaching here is a failure of the gate under test.
    return new Response(JSON.stringify({ publication: { view_url: 'https://drive/x' } }), { status: 200 })
  })
}

describe('PublishHub — a core destination reaches the exposure acknowledgment', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })

  it('lists the core destination by its display_name', async () => {
    mockRegistries(fetchSpy, [coreDescriptor()])
    render(<PublishHub artifact={fakeArtifact} />, { wrapper })
    expect(await screen.findByText('Public web (shared drive)')).toBeTruthy()
  })

  it('does NOT publish on the first Publish click -- it asks first', async () => {
    mockRegistries(fetchSpy, [coreDescriptor()])
    render(<PublishHub artifact={fakeArtifact} />, { wrapper })
    fireEvent.click(await screen.findByText('Public web (shared drive)'))
    fireEvent.click(await screen.findByRole('button', { name: /publish/i }))
    await waitFor(() => {
      const posted = fetchSpy.mock.calls.filter(
        c => String(c[0]).includes('/publish') && (c[1] as RequestInit | undefined)?.method === 'POST',
      )
      expect(posted).toHaveLength(0)
    })
  })

  it('offers no TTL control for a destination that cannot expire a link', async () => {
    mockRegistries(fetchSpy, [coreDescriptor()])
    render(<PublishHub artifact={fakeArtifact} />, { wrapper })
    fireEvent.click(await screen.findByText('Public web (shared drive)'))
    await waitFor(() => expect(screen.queryByLabelText(/time to live/i)).toBeNull())
  })

  it('shows the remedy on an unavailable destination without leaving the list', async () => {
    // The hint was previously gated on the row being selected, while the list itself only
    // renders when nothing is selected -- mutually exclusive, so it could never appear.
    mockRegistries(fetchSpy, [
      coreDescriptor({ available: false, install_hint: 'Register an AWS profile first.' }),
    ])
    render(<PublishHub artifact={fakeArtifact} />, { wrapper })
    expect(await screen.findByText('Register an AWS profile first.')).toBeTruthy()
  })

  it('tells a core destination the truth about how the exposure ends', async () => {
    // The modal's default persistent sentence names `recall` and `destroy`, which are the
    // deploy surface's actions. This destination has neither, so consent copy carrying
    // them would point the user at a way out that does not exist for it.
    mockRegistries(fetchSpy, [coreDescriptor()])
    render(<PublishHub artifact={fakeArtifact} />, { wrapper })
    fireEvent.click(await screen.findByText('Public web (shared drive)'))
    fireEvent.click(await screen.findByRole('button', { name: /publish/i }))
    fireEvent.click(await screen.findByRole('button', { name: /confirm/i }))
    const dialog = await screen.findByRole('dialog')
    // Names the ONE exit that exists in the dashboard -- deleting the artifact -- WITHOUT
    // promising the exposure ends at that moment. It previously required "private or
    // unpublish", which swapped one unavailable way out for another (`api.unpublishArtifact`
    // and `api.updateArtifactSharing` are defined but called from zero components); it then
    // said "stays public until you delete the artifact", which a delete against an
    // unreachable destination makes false -- that delete proceeds locally and leaves the
    // copy public. Note `/delet/` not `/delete/`: the copy reads "Deleting".
    expect(dialog.textContent).toMatch(/delet/i)
    expect(dialog.textContent).toMatch(/withdraw/i)
    expect(dialog.textContent).not.toMatch(/until you delete/i)
    expect(dialog.textContent).not.toMatch(/private or unpublish/i)
    expect(dialog.textContent).not.toMatch(/recall or destroy/i)
  })
})
