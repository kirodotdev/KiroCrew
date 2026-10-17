/**
 * The /artifacts page's chat scopes: `?touched_by=<slot>` and `?chat_folder=<id>`.
 *
 * Each scope is passed to the list API (the server resolves it) and renders as a
 * dismissible chip; dismissing it drops the parameter and restores the whole
 * library. With neither parameter the page asks for, and shows, exactly the
 * unscoped list.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, cleanup, fireEvent } from '@testing-library/react'
import ArtifactsPage from '../pages/ArtifactsPage'
import { createArtifactsEndpoints } from '../api/client/artifacts'
import { createTestStore, renderWithProviders } from './helpers'
import { sseSlots } from '../store/dashboardSlice'
import { api } from '../api/client'

vi.mock('../api/client')
vi.mock('@virtuoso.dev/masonry', () => ({ VirtuosoMasonry: () => <div data-testid="masonry" /> }))

const artifact = (slug: string, name: string) => ({
  slug,
  name,
  kind: 'widget',
  version: 1,
  updated_at: new Date().toISOString(),
  created_at: new Date().toISOString(),
  tags: [],
  description: '',
  source: 'chat',
})

type ListFilters = { touchedBy?: string; chatFolder?: string }

/** The library holds two artifacts; the chat scopes narrow it to one. */
function mockLibrary() {
  vi.mocked(api).artifacts = vi.fn().mockImplementation((filters?: ListFilters) =>
    Promise.resolve({
      artifacts: filters?.touchedBy || filters?.chatFolder
        ? [artifact('scoped', 'Scoped Report')]
        : [artifact('scoped', 'Scoped Report'), artifact('other', 'Other Widget')],
    }),
  )
  vi.mocked(api).artifactFolders = vi.fn().mockResolvedValue({ folders: [] })
  vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({ docs: [] })
  vi.mocked(api).chatFolders = vi.fn().mockResolvedValue([{ id: 'f-ops', name: 'Ops', order: 0 }])
}

function listCalls(): ListFilters[] {
  return vi.mocked(api.artifacts).mock.calls.map(c => (c[0] ?? {}) as ListFilters)
}

describe('ArtifactsPage chat scopes', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.setItem('mc-artifacts-view', 'grid')
    localStorage.setItem('mc-artifacts-pinned-only', '0')
    mockLibrary()
  })
  afterEach(cleanup)

  it('with no scope parameter lists the whole library and shows no chip', async () => {
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts' })
    expect(await screen.findByText('Other Widget')).toBeInTheDocument()
    expect(screen.getByText('Scoped Report')).toBeInTheDocument()
    expect(screen.queryByTestId('artifact-scope-chips')).not.toBeInTheDocument()
    for (const f of listCalls()) {
      expect(f.touchedBy).toBeUndefined()
      expect(f.chatFolder).toBeUndefined()
    }
  })

  it('scopes to a chat folder, names it in a chip, and clearing restores the library', async () => {
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?chat_folder=f-ops' })
    expect(await screen.findByText('From chats in Ops')).toBeInTheDocument()
    await waitFor(() => expect(listCalls().some(f => f.chatFolder === 'f-ops')).toBe(true))
    expect(await screen.findByText('Scoped Report')).toBeInTheDocument()
    expect(screen.queryByText('Other Widget')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Clear filter: From chats in Ops' }))
    expect(await screen.findByText('Other Widget')).toBeInTheDocument()
    expect(screen.queryByTestId('artifact-scope-chips')).not.toBeInTheDocument()
  })

  it('labels a folder it cannot name with a generic chip', async () => {
    vi.mocked(api).chatFolders = vi.fn().mockResolvedValue([])
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?chat_folder=f-gone' })
    expect(await screen.findByText('From chats in a sidebar folder')).toBeInTheDocument()
    expect(screen.queryByText(/Couldn't load the sidebar folders/)).not.toBeInTheDocument()
  })

  it('reports a folder list that fails to load instead of only falling back', async () => {
    vi.mocked(api).chatFolders = vi.fn().mockRejectedValue(new Error('HTTP 500'))
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?chat_folder=f-ops' })
    expect(await screen.findByText(/Couldn't load the sidebar folders/)).toBeInTheDocument()
    expect(screen.getByText('From chats in a sidebar folder')).toBeInTheDocument()
    await waitFor(() => expect(listCalls().some(f => f.chatFolder === 'f-ops')).toBe(true))
  })

  it('scopes to one chat and names it by its title', async () => {
    const store = createTestStore()
    store.dispatch(sseSlots([{ key: 'chat-1', title: 'Design review', messages: 1, running: false }] as never))
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?touched_by=chat-1', store })
    expect(await screen.findByText('From chat Design review')).toBeInTheDocument()
    await waitFor(() => expect(listCalls().some(f => f.touchedBy === 'chat-1')).toBe(true))
    expect(await screen.findByText('Scoped Report')).toBeInTheDocument()
    expect(screen.queryByText('Other Widget')).not.toBeInTheDocument()
  })

  it('says nothing matches, not that the library is empty, when a scope matches nothing', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?chat_folder=f-ops' })
    expect(await screen.findByText('No artifacts match your filters.')).toBeInTheDocument()
  })
})

describe('artifacts list client: chat scopes', () => {
  function client() {
    const get = vi.fn().mockResolvedValue({})
    const endpoints = createArtifactsEndpoints({
      get,
      post: vi.fn(),
      del: vi.fn(),
      patch: vi.fn(),
      j: (r: unknown) => r,
      jfetch: vi.fn(),
      checkSessionExpired: vi.fn(),
      removeAuthBanner: vi.fn(),
    } as never)
    return { get, endpoints: endpoints.library }
  }

  it('sends chat_folder only when one is named', async () => {
    const { get, endpoints } = client()
    await endpoints.artifacts({ chatFolder: 'f-ops' })
    expect(get).toHaveBeenLastCalledWith('/api/artifacts?chat_folder=f-ops')
    await endpoints.artifacts({})
    expect(get).toHaveBeenLastCalledWith('/api/artifacts')
  })
})
