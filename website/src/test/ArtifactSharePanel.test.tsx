import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ArtifactSharePanel } from '../components/ArtifactSharePanel'
import { api } from '../api/client'
import type { Artifact, PublishProviderDescriptor } from '../types'

vi.mock('../api/client', () => ({
  api: {
    publishArtifact: vi.fn(),
    updateArtifactSharing: vi.fn(),
    unpublishArtifact: vi.fn(),
    getArtifactPublishProviders: vi.fn(),
  },
}))

const writeText = vi.fn().mockResolvedValue(undefined)
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter>
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  </MemoryRouter>
)

const ARTIFACTORY_DESC: PublishProviderDescriptor = {
  name: 'artifactory',
  display_name: 'Artifactory',
  capabilities: ['content_versions', 'sharing'],
  kind_support: 'native',
  capable: true,
  sharing_model: {
    supports_private: true,
    supports_shared: true,
    supports_public: true,
    principal_kind: 'alias',
    supports_roles: true,
    supports_expiration: false,
    programmable: true,
    out_of_band_url: '',
  },
  sync_model: { authority: 'kirocrew', concurrency: 'token', collab_mode: 'mirror' },
  discovery_model: {
    list_mine: true,
    list_shared_with_me: true,
    list_public: true,
    full_text_search: false,
    pull_by_id: true,
  },
}

const CHORUS_DESC: PublishProviderDescriptor = {
  name: 'chorus',
  display_name: 'Chorus',
  capabilities: ['content_versions', 'comments_write', 'realtime'],
  kind_support: 'native',
  capable: true,
  sharing_model: {
    supports_private: true,
    supports_shared: true,
    supports_public: false,
    principal_kind: 'alias',
    supports_roles: false,
    supports_expiration: false,
    programmable: false,
    out_of_band_url: 'https://chorus.aws.dev/doc/{external_id}',
  },
  sync_model: { authority: 'remote', concurrency: 'crdt', collab_mode: 'live' },
  discovery_model: {
    list_mine: true,
    list_shared_with_me: false,
    list_public: false,
    full_text_search: true,
    pull_by_id: true,
  },
}

const MARKBIN_DESC: PublishProviderDescriptor = {
  name: 'markbin',
  display_name: 'MarkBin',
  capabilities: ['content_versions', 'content_pull', 'sharing', 'comments_write'],
  kind_support: 'native',
  capable: true,
  sharing_model: {
    supports_private: true,
    supports_shared: false,
    supports_public: true,
    principal_kind: 'none',
    supports_roles: false,
    supports_expiration: true,
    programmable: true,
    out_of_band_url: '',
  },
  sync_model: { authority: 'kirocrew', concurrency: 'lww', collab_mode: 'mirror' },
  discovery_model: {
    list_mine: true,
    list_shared_with_me: false,
    list_public: false,
    full_text_search: true,
    pull_by_id: true,
  },
}

function makeArtifact(over: Partial<Artifact> = {}): Artifact {
  return {
    slug: 'doc',
    name: 'Doc',
    kind: 'text',
    source: 'manual',
    description: '',
    tags: [],
    version: 3,
    created_at: '',
    updated_at: '',
    ...over,
  }
}

function mockProviders(providers: PublishProviderDescriptor[]) {
  vi.mocked(api).getArtifactPublishProviders = vi
    .fn()
    .mockResolvedValue({ providers, kind: 'text' })
}

beforeEach(() => {
  writeText.mockClear()
  queryClient.clear()
  Object.assign(navigator, { clipboard: { writeText } })
  vi.mocked(api).publishArtifact = vi.fn().mockResolvedValue({})
  vi.mocked(api).updateArtifactSharing = vi.fn().mockResolvedValue({})
  vi.mocked(api).unpublishArtifact = vi.fn().mockResolvedValue({})
  mockProviders([ARTIFACTORY_DESC])
})

describe('ArtifactSharePanel — unpublished', () => {
  it('publishes PRIVATE by default (to Artifactory)', async () => {
    render(<ArtifactSharePanel artifact={makeArtifact()} />, { wrapper })
    fireEvent.click(screen.getByText('Publish to Artifactory'))
    await waitFor(() =>
      expect(api.publishArtifact).toHaveBeenCalledWith('doc', {
        visibility: 'PRIVATE',
        shared_with: [],
        provider: 'artifactory',
      }),
    )
  })

  it('disables Publish for SHARED until an alias is added', async () => {
    render(<ArtifactSharePanel artifact={makeArtifact()} />, { wrapper })
    fireEvent.click(screen.getByText('Shared'))
    const publishBtn = screen.getByText('Publish to Artifactory') as HTMLButtonElement
    expect(publishBtn.disabled).toBe(true)
    const input = screen.getByLabelText('Add an alias to share with')
    fireEvent.change(input, { target: { value: 'alice' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect((screen.getByText('Publish to Artifactory') as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(screen.getByText('Publish to Artifactory'))
    await waitFor(() =>
      expect(api.publishArtifact).toHaveBeenCalledWith('doc', {
        visibility: 'SHARED',
        shared_with: ['alice'],
        provider: 'artifactory',
      }),
    )
  })

  it('requires confirmation before publishing PUBLIC', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<ArtifactSharePanel artifact={makeArtifact()} />, { wrapper })
    fireEvent.click(screen.getByText('Public'))
    fireEvent.click(screen.getByText('Publish to Artifactory'))
    expect(confirmSpy).toHaveBeenCalled()
    expect(api.publishArtifact).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it('shows a provider picker and publishes to Chorus (PRIVATE, no visibility controls)', async () => {
    mockProviders([ARTIFACTORY_DESC, CHORUS_DESC])
    render(<ArtifactSharePanel artifact={makeArtifact()} />, { wrapper })
    // Picker appears once providers load.
    const chorusBtn = await screen.findByRole('button', { name: /^Chorus$/ })
    fireEvent.click(chorusBtn)
    // Chorus is not programmable → no Public/Shared visibility buttons, just a note.
    expect(screen.queryByText('Public')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('Publish to Chorus'))
    await waitFor(() =>
      expect(api.publishArtifact).toHaveBeenCalledWith('doc', {
        visibility: 'PRIVATE',
        shared_with: [],
        provider: 'chorus',
      }),
    )
  })

  it('warns when the selected provider degrades the artifact kind', async () => {
    mockProviders([{ ...CHORUS_DESC, kind_support: 'degraded' }])
    render(<ArtifactSharePanel artifact={makeArtifact({ kind: 'widget' })} />, { wrapper })
    expect(await screen.findByText(/won.t render there/i)).toBeInTheDocument()
  })

  it('hides "Shared" for a link-based provider (supports_shared=false)', async () => {
    mockProviders([MARKBIN_DESC])
    render(<ArtifactSharePanel artifact={makeArtifact()} />, { wrapper })
    expect(await screen.findByText('Publish to MarkBin')).toBeInTheDocument()
    expect(screen.getByText('Private')).toBeInTheDocument()
    expect(screen.getByText('Public')).toBeInTheDocument()
    expect(screen.queryByText('Shared')).not.toBeInTheDocument()
  })
})

describe('ArtifactSharePanel — published (Artifactory / mirror)', () => {
  const published = makeArtifact({
    publication: {
      artifact_id: 'uuid-1',
      view_url: 'https://artifactory.beta.harmony.a2z.com/artifact/uuid-1',
      provider: 'artifactory',
      collab_mode: 'mirror',
      visibility: 'PRIVATE',
      shared_with: [],
      auto_sync: true,
      last_synced_kirocrew_version: 3,
      version_map: { '3': 2 },
      published_at: '',
      published_by: 'nrb',
      last_error: '',
    },
  })

  it('shows the stable link and copies it', async () => {
    render(<ArtifactSharePanel artifact={published} />, { wrapper })
    const link = screen.getByLabelText('Share link') as HTMLInputElement
    expect(link.value).toBe('https://artifactory.beta.harmony.a2z.com/artifact/uuid-1')
    fireEvent.click(screen.getByLabelText('Copy link'))
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(
        'https://artifactory.beta.harmony.a2z.com/artifact/uuid-1',
      ),
    )
  })

  it('shows the version-sync line', () => {
    render(<ArtifactSharePanel artifact={published} />, { wrapper })
    expect(screen.getByText(/KiroCrew v3/)).toBeInTheDocument()
    expect(screen.getByText(/Artifactory v2/)).toBeInTheDocument()
  })

  it('renders an http(s) view_url as a real "Open in" href', () => {
    render(<ArtifactSharePanel artifact={published} />, { wrapper })
    const open = screen.getByLabelText('Open in Artifactory') as HTMLAnchorElement
    expect(open.getAttribute('href')).toBe(
      'https://artifactory.beta.harmony.a2z.com/artifact/uuid-1',
    )
  })

  it('neutralizes a javascript: view_url (no dangerous href, link disabled)', () => {
    // A provider-controlled view_url with a dangerous scheme must never become a
    // clickable href (stored-XSS vector). The "Open in" affordance is disabled.
    const evil = makeArtifact({
      publication: {
        ...published.publication!,
        // eslint-disable-next-line no-script-url
        view_url: 'javascript:alert(document.cookie)',
      },
    })
    render(<ArtifactSharePanel artifact={evil} />, { wrapper })
    const open = screen.getByLabelText('Link unavailable (unsafe URL)') as HTMLAnchorElement
    expect(open.getAttribute('href')).toBeNull()
  })

  it('unpublishes after confirmation', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<ArtifactSharePanel artifact={published} />, { wrapper })
    fireEvent.click(screen.getByText('Unpublish'))
    await waitFor(() => expect(api.unpublishArtifact).toHaveBeenCalledWith('doc'))
    confirmSpy.mockRestore()
  })

  it('surfaces a conflict banner with a force re-sync action', async () => {
    const conflicted = makeArtifact({
      publication: { ...published.publication!, last_error: 'conflict: changed out-of-band' },
    })
    render(<ArtifactSharePanel artifact={conflicted} />, { wrapper })
    expect(screen.getByText(/conflict: changed out-of-band/)).toBeInTheDocument()
    fireEvent.click(screen.getByText('Force re-sync'))
    await waitFor(() => expect(api.publishArtifact).toHaveBeenCalled())
  })
})

describe('ArtifactSharePanel — published (Chorus / live)', () => {
  const live = makeArtifact({
    publication: {
      artifact_id: 'docABC',
      view_url: 'https://chorus.aws.dev/doc/docABC',
      provider: 'chorus',
      collab_mode: 'live',
      visibility: 'PRIVATE',
      shared_with: [],
      auto_sync: true,
      last_synced_kirocrew_version: 3,
      version_map: { '3': 2 },
      published_at: '',
      published_by: 'nrb',
      last_error: 'sync failed: boom',
    },
  })

  it('hides Force re-sync and visibility controls, shows the manage-in-Chorus link', () => {
    render(<ArtifactSharePanel artifact={live} />, { wrapper })
    expect(screen.getByText('Published to Chorus')).toBeInTheDocument()
    // CRDT → no force re-sync even though last_error is set; no visibility switcher.
    expect(screen.queryByText('Force re-sync')).not.toBeInTheDocument()
    expect(screen.queryByText('Public')).not.toBeInTheDocument()
    expect(screen.queryByText('Un-share')).not.toBeInTheDocument()
    expect(screen.getByText(/Manage sharing in Chorus/)).toBeInTheDocument()
    expect(screen.getByText(/live in Chorus/)).toBeInTheDocument()
  })

  it('can still unpublish a live doc', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<ArtifactSharePanel artifact={live} />, { wrapper })
    fireEvent.click(screen.getByText('Unpublish'))
    await waitFor(() => expect(api.unpublishArtifact).toHaveBeenCalledWith('doc'))
    confirmSpy.mockRestore()
  })
})

describe('ArtifactSharePanel — errors', () => {
  it('renders the server error message, not raw JSON', async () => {
    vi.mocked(api).publishArtifact = vi
      .fn()
      .mockRejectedValue(new Error('400: {"error":"SHARED visibility requires at least one alias in shared_with"}'))
    render(<ArtifactSharePanel artifact={makeArtifact()} />, { wrapper })
    fireEvent.click(screen.getByText('Publish to Artifactory'))
    await waitFor(() =>
      expect(
        screen.getByText('SHARED visibility requires at least one alias in shared_with'),
      ).toBeInTheDocument(),
    )
    expect(screen.queryByText(/\{"error"/)).not.toBeInTheDocument()
  })
})
