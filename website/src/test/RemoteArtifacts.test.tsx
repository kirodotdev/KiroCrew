import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ArtifactsPage from '../pages/ArtifactsPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Artifact, PublishProviderDescriptor, RemoteArtifact } from '../types'

vi.mock('../api/client')

// VirtuosoMasonry virtualizes against real layout, which jsdom lacks — mock it
// to a plain map so card content renders (same shim as ArtifactsPage.test.tsx).
vi.mock('@virtuoso.dev/masonry', () => ({
  VirtuosoMasonry: ({ data, context, ItemContent }: any) => (
    <div data-testid="masonry">
      {data.map((d: any, i: number) => (
        <ItemContent key={i} data={d} index={i} context={context} />
      ))}
    </div>
  ),
}))

const mkArtifact = (slug: string, overrides: Partial<Artifact> = {}): Artifact => ({
  slug,
  name: slug.replace(/-/g, ' '),
  kind: 'widget',
  source: 'chat',
  // The library defaults to the "Starred" view (pinnedOnly), so a fixture must
  // be pinned to render as a card. Mirrors ArtifactsPage.test.tsx's helper.
  pinned: true,
  description: '',
  tags: [],
  version: 1,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:00:00.000000+00:00',
  ...overrides,
})

const mkProvider = (name: string, overrides: Partial<PublishProviderDescriptor> = {}): PublishProviderDescriptor => ({
  name,
  display_name: `${name[0].toUpperCase()}${name.slice(1)} Provider`,
  capabilities: ['content_versions', 'sharing'],
  kind_support: 'native',
  capable: true,
  sharing_model: {
    supports_private: true,
    supports_shared: true,
    supports_public: true,
    principal_kind: 'user',
    supports_roles: false,
    supports_expiration: false,
    programmable: true,
    out_of_band_url: '',
  },
  sync_model: { authority: 'mirror', concurrency: 'token', collab_mode: 'mirror' },
  discovery_model: {
    list_mine: true,
    list_shared_with_me: true,
    list_public: true,
    full_text_search: false,
    pull_by_id: true,
  },
  ...overrides,
})

const mkRemote = (id: string): RemoteArtifact => ({
  external_id: id,
  title: `Remote ${id}`,
  owner: 'someone',
  view_url: `https://remote.example.com/a/${id}`,
  updated_at: '2026-07-01T00:00:00Z',
  snippet: '',
  tags: [],
  local_slug: null,
})

describe('ArtifactsPage remote-browse gating', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [mkArtifact('local-a')] })
  })

  it('renders NO remote section when the provider registry is empty (public edition)', async () => {
    vi.mocked(api).getArtifactPublishProviders = vi.fn().mockResolvedValue({ providers: [], kind: 'widget' })
    const browse = vi.fn()
    vi.mocked(api).browseRemoteArtifacts = browse
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('local a')).toBeInTheDocument())
    // Zero remote pixels AND zero remote requests.
    expect(screen.queryByText(/^On /)).not.toBeInTheDocument()
    expect(browse).not.toHaveBeenCalled()
  })

  it('renders a browse section per registered discovery-capable provider', async () => {
    vi.mocked(api).getArtifactPublishProviders = vi.fn().mockResolvedValue({
      providers: [mkProvider('companion')],
      kind: 'widget',
    })
    vi.mocked(api).browseRemoteArtifacts = vi.fn().mockResolvedValue({ artifacts: [mkRemote('ext-1')] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('On Companion Provider')).toBeInTheDocument())
    expect(screen.getByText('Remote ext-1')).toBeInTheDocument()
    // The browse call was routed by provider name, not a hardcoded vendor.
    expect(vi.mocked(api).browseRemoteArtifacts).toHaveBeenCalledWith('companion', { scope: 'mine' })
  })

  it('dedups rows that already exist locally (local_slug set)', async () => {
    vi.mocked(api).getArtifactPublishProviders = vi.fn().mockResolvedValue({
      providers: [mkProvider('companion')],
      kind: 'widget',
    })
    vi.mocked(api).browseRemoteArtifacts = vi.fn().mockResolvedValue({
      artifacts: [{ ...mkRemote('ext-1'), local_slug: 'local-a' }, mkRemote('ext-2')],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('Remote ext-2')).toBeInTheDocument())
    expect(screen.queryByText('Remote ext-1')).not.toBeInTheDocument()
  })

  it('hides the remote section entirely when the provider browse errors', async () => {
    vi.mocked(api).getArtifactPublishProviders = vi.fn().mockResolvedValue({
      providers: [mkProvider('companion')],
      kind: 'widget',
    })
    vi.mocked(api).browseRemoteArtifacts = vi.fn().mockRejectedValue(new Error('remote down'))
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('local a')).toBeInTheDocument())
    expect(screen.queryByText('On Companion Provider')).not.toBeInTheDocument()
  })

  it('full-text search keeps the filter input focused and mounted across keystrokes', async () => {
    // full_text_search=true routes each keystroke through the /browse?q= query
    // (its own query key). keepPreviousData must keep the section mounted so
    // the SearchInput retains focus mid-word instead of unmounting per char.
    vi.mocked(api).getArtifactPublishProviders = vi.fn().mockResolvedValue({
      providers: [mkProvider('companion', {
        discovery_model: {
          list_mine: true, list_shared_with_me: false, list_public: false,
          full_text_search: true, pull_by_id: true,
        },
      })],
      kind: 'widget',
    })
    let call = 0
    vi.mocked(api).browseRemoteArtifacts = vi.fn().mockImplementation(async () => {
      call += 1
      return { artifacts: [mkRemote(`ext-${call}`)] }
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('On Companion Provider')).toBeInTheDocument())

    const input = screen.getByPlaceholderText(/Filter Companion Provider artifacts/i) as HTMLInputElement
    input.focus()
    expect(document.activeElement).toBe(input)
    await userEvent.type(input, 'dash')
    // The input survived every keystroke (never unmounted) and kept focus.
    expect(document.activeElement).toBe(input)
    expect(input.value).toBe('dash')
  })

  it('paginates: a next_page_token surfaces Load more, which fetches the next page', async () => {
    vi.mocked(api).getArtifactPublishProviders = vi.fn().mockResolvedValue({
      providers: [mkProvider('companion')],
      kind: 'widget',
    })
    // Page 1 hands out a next_page_token; page 2 is terminal (no token). Without
    // the pageToken wiring the second page would be unreachable.
    vi.mocked(api).browseRemoteArtifacts = vi.fn().mockImplementation(async (_p, opts) => {
      if (opts?.pageToken === 'tok-2') {
        return { artifacts: [mkRemote('ext-2')], next_page_token: null }
      }
      return { artifacts: [mkRemote('ext-1')], next_page_token: 'tok-2' }
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('Remote ext-1')).toBeInTheDocument())
    // Page 2 is not fetched until the user asks for it.
    expect(screen.queryByText('Remote ext-2')).not.toBeInTheDocument()

    // The control carries a stable accessible name (aria-label) so it is
    // identifiable even in its loading state where it renders only a spinner.
    const loadMore = screen.getByRole('button', { name: /load more companion provider artifacts/i })
    await userEvent.click(loadMore)
    await waitFor(() => expect(screen.getByText('Remote ext-2')).toBeInTheDocument())
    // Both pages now show, and the token was forwarded to the provider.
    expect(screen.getByText('Remote ext-1')).toBeInTheDocument()
    expect(vi.mocked(api).browseRemoteArtifacts).toHaveBeenCalledWith('companion', {
      scope: 'mine',
      pageToken: 'tok-2',
    })
    // Terminal page dropped the token → no more Load more.
    expect(screen.queryByRole('button', { name: /load more/i })).not.toBeInTheDocument()
  })
})
