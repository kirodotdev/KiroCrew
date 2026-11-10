/**
 * The Artifacts page on a desktop: the side list (Home, All artifacts, the
 * folder tree) and Home's two groups.
 *
 * Home splits on the server's own `sweepable` flag: Recent is everything the
 * sweep can never delete, Recent output is what it could, plus unsaved session
 * documents. These tests pin that split, the cap wording, keeping an output
 * by its star, and the side list driving the URL.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type * as React from 'react'
import { screen, waitFor, within, cleanup, renderHook, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import ArtifactsPage from '../pages/ArtifactsPage'
import { useMoveArtifactToFolder } from '../hooks/useArtifactFolders'
import { renderWithProviders, createTestStore } from './helpers'
import { sseConnected } from '../store/dashboardSlice'
import { selectionState } from '../guide/guidePredicates'
import { api } from '../api/client'
import type { Artifact, ArtifactFolder, SessionDoc } from '../types'

vi.mock('../api/client')
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
vi.mock('@virtuoso.dev/masonry', () => ({ VirtuosoMasonry: () => <div data-testid="masonry" /> }))

const mkArtifact = (slug: string, overrides: Partial<Artifact> = {}): Artifact => ({
  slug,
  name: slug.replace(/-/g, ' '),
  kind: 'widget',
  source: 'chat',
  pinned: false,
  description: '',
  tags: [],
  version: 1,
  created_at: '2026-11-01T10:00:00+00:00',
  updated_at: '2026-11-01T10:00:00+00:00',
  ...overrides,
})

const mkFolder = (id: string, name: string, overrides: Partial<ArtifactFolder> = {}): ArtifactFolder => ({
  id, name, order: 0, parent_id: '', item_count: 0, ...overrides,
})

const doc: SessionDoc = {
  path: '/work/plan.md',
  name: 'plan.md',
  updated_at: '2026-11-02T10:00:00',
  session_key: 'dashboard_chat-1',
  session_title: 'Doc indicators',
  message_ts: 'm1',
  saved: false,
  slug: '',
}

function seed({ artifacts = [] as Artifact[], folders = [] as ArtifactFolder[], docs = [] as SessionDoc[], cap = 200 as number | null } = {}) {
  const m = vi.mocked(api)
  m.artifacts = vi.fn().mockResolvedValue({ artifacts, ...(cap === null ? {} : { output_cap: cap }) })
  m.artifactFolders = vi.fn().mockResolvedValue({ folders })
  m.artifactSessionDocs = vi.fn().mockResolvedValue({ docs })
  m.getArtifactPublishProviders = vi.fn().mockResolvedValue({ providers: [], kind: 'widget' })
  m.publishProviders = vi.fn().mockResolvedValue({ providers: [] })
  m.themeBoot = vi.fn().mockResolvedValue({})
  m.artifact = vi.fn().mockImplementation((slug: string) =>
    Promise.resolve(artifacts.find((a) => a.slug === slug) ?? mkArtifact(slug)))
  m.setArtifactPinned = vi.fn().mockResolvedValue({})
  return m
}

const home = () => screen.getByTestId('artifacts-home')
const group = (title: string) => within(home()).getByRole('heading', { name: title }).closest('section') as HTMLElement

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }))
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('Artifacts Home', () => {
  it('splits made-or-kept from output on the server sweep flag', async () => {
    seed({
      artifacts: [
        mkArtifact('red-main-audit', { kind: 'markdown' }),
        mkArtifact('starred-widget', { auto_registered: true, pinned: true, sweepable: false }),
        mkArtifact('chat-widget', { auto_registered: true, sweepable: true }),
      ],
      docs: [doc],
    })
    renderWithProviders(<ArtifactsPage />)

    await waitFor(() => expect(within(group('Recent')).getByText('red main audit')).toBeInTheDocument())
    const recent = group('Recent')
    expect(within(recent).getByText('starred widget')).toBeInTheDocument()
    expect(within(recent).queryByText('chat widget')).toBeNull()

    const output = group('Recent output')
    expect(within(output).getByText('chat widget')).toBeInTheDocument()
    // An unsaved session document is output too, though it is no library record.
    expect(within(output).getByText('plan.md')).toBeInTheDocument()
    expect(within(output).queryByText('red main audit')).toBeNull()
  })

  it('says how much output there is against the sweep cap', async () => {
    seed({ artifacts: [mkArtifact('a-one', { sweepable: true }), mkArtifact('a-two', { sweepable: true })], cap: 200 })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(within(home()).getByText(/2 of 200 before the oldest clear/)).toBeInTheDocument())
  })

  it('falls back to wording with no number when the gateway sends no cap', async () => {
    seed({ artifacts: [mkArtifact('a-one', { sweepable: true })], cap: null })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(within(home()).getByText(/clears as newer output arrives/)).toBeInTheDocument())
  })

  it('keeps an output by starring it, without opening it', async () => {
    const user = userEvent.setup()
    const m = seed({ artifacts: [mkArtifact('chat-widget', { auto_registered: true, sweepable: true })] })
    renderWithProviders(<ArtifactsPage />)
    const output = await waitFor(() => group('Recent output'))
    await user.click(within(output).getByRole('button', { name: 'Star to keep' }))
    expect(m.setArtifactPinned).toHaveBeenCalledWith('chat-widget', true)
  })

  it('puts the two groups side by side in the table view', async () => {
    localStorage.setItem('mc-artifacts-view', 'table')
    seed({ artifacts: [mkArtifact('kept-doc', { kind: 'markdown' }), mkArtifact('chat-widget', { sweepable: true })] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(within(group('Recent')).getByText('kept doc')).toBeInTheDocument())
    expect(home().className).toMatch(/lg:grid-cols-2/)
    expect(within(group('Recent output')).getByText('chat widget')).toBeInTheDocument()
  })

  it('offers no Starred filter on Home, since Home already splits kept from not', async () => {
    seed({ artifacts: [mkArtifact('kept-doc')] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(home()).toBeInTheDocument())
    expect(screen.queryByRole('group', { name: /starred/i })).toBeNull()
  })
})

describe('Artifacts desktop views keep a guide target and two controls a card', () => {
  it('wraps Home in the artifacts.list picker region', async () => {
    seed({ artifacts: [mkArtifact('kept-doc')] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(home()).toBeInTheDocument())
    const region = document.querySelector('[data-ui-location="artifacts.list"]')
    expect(region).not.toBeNull()
    expect(region!.contains(home())).toBe(true)
  })

  it('offers a guide pick on Home when every artifact is filed in a folder', async () => {
    seed({ artifacts: [mkArtifact('filed-doc', { folder_id: 'ops' })], folders: [mkFolder('ops', 'Ops')] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(within(home()).getByText('filed doc')).toBeInTheDocument())
    // Home draws the filed record, so the picker is not empty.
    await waitFor(() => expect(selectionState('artifact_open')).toBe('none'))
  })

  it('says a preview that could not be read instead of drawing it blank', async () => {
    const m = seed({ artifacts: [mkArtifact('broken-doc', { kind: 'markdown' })] })
    m.artifact = vi.fn().mockRejectedValue(new Error('content unreadable'))
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(within(home()).getByText('content unreadable')).toBeInTheDocument())
    expect(within(home()).getByText("Couldn't load this preview")).toBeInTheDocument()
    // Tiles sit in click-through preview areas and under a card's open button,
    // so the failed tile takes pointer events back and stacks above it, or its
    // hand-off would never receive the click.
    const tile = within(home()).getByTestId('artifact-preview-error')
    expect(tile.className).toMatch(/\bpointer-events-auto\b/)
    expect(tile.className).toMatch(/\bz-10\b/)
  })

  it('folds pop-out and remove into one menu beside the star', async () => {
    const user = userEvent.setup()
    seed({ artifacts: [mkArtifact('kept-doc')] })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?scope=all' })
    const grid = await waitFor(() => screen.getByTestId('artifacts-card-grid'))
    expect(within(grid).queryByRole('button', { name: /pop out/i })).toBeNull()
    await user.click(within(grid).getByRole('button', { name: 'More actions' }))
    expect(await screen.findByRole('menuitem', { name: /pop out/i })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /remove/i })).toBeInTheDocument()
  })

  it('keeps the publication mark, description and tags the masonry card showed', async () => {
    seed({
      artifacts: [mkArtifact('shared-doc', {
        description: 'Weekly numbers',
        tags: ['ops'],
        publication: {
          artifact_id: 'r1', view_url: 'https://example.com/artifact/r1', provider: 'example',
          visibility: 'SHARED', shared_with: [], auto_sync: false,
          last_synced_kirocrew_version: 1, version_map: { 1: 1 },
        } as unknown as Artifact['publication'],
      })],
    })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?scope=all' })
    const grid = await waitFor(() => screen.getByTestId('artifacts-card-grid'))
    expect(within(grid).getByTestId('publication-badge')).toBeInTheDocument()
    expect(within(grid).getByText('Weekly numbers')).toBeInTheDocument()
    expect(within(grid).getByText('ops')).toBeInTheDocument()
  })

  it('keeps a side-list rename open while the same folder shows in the main column', async () => {
    const user = userEvent.setup()
    localStorage.setItem('mc-artifacts-view', 'table')
    seed({ folders: [mkFolder('ops', 'Ops')] })
    // The folder shows in the side list; the rename field opens only there.
    // Rename is gateway-gated, so the store must be connected.
    const store = createTestStore()
    store.dispatch(sseConnected())
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?scope=all', store })
    const nav = await waitFor(() => screen.getByRole('navigation', { name: 'Artifact library' }))
    await user.click(await waitFor(() => within(nav).getByRole('button', { name: 'Actions for folder Ops' })))
    await user.click(await screen.findByRole('menuitem', { name: /rename/i }))
    await waitFor(() => expect(within(nav).getByDisplayValue('Ops')).toBeInTheDocument())
    expect(screen.getAllByDisplayValue('Ops')).toHaveLength(1)
  })
})

describe('Artifacts side list', () => {
  it('lists the folder tree with direct counts and opens a folder', async () => {
    const user = userEvent.setup()
    seed({
      artifacts: [mkArtifact('filed-one', { folder_id: 'ops' }), mkArtifact('loose-one')],
      folders: [mkFolder('ops', 'Ops')],
    })
    renderWithProviders(<ArtifactsPage />)
    const nav = await waitFor(() => screen.getByRole('navigation', { name: 'Artifact library' }))
    const ops = await waitFor(() => within(nav).getByRole('button', { name: /^Ops/ }))
    expect(ops.textContent).toContain('1')

    await user.click(ops)
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Ops' })).toBeInTheDocument())
    expect(screen.queryByTestId('artifacts-home')).toBeNull()
  })

  it('shows every artifact flat under All artifacts, filed or not', async () => {
    const user = userEvent.setup()
    seed({
      artifacts: [mkArtifact('filed-one', { folder_id: 'ops' }), mkArtifact('loose-one')],
      folders: [mkFolder('ops', 'Ops')],
    })
    localStorage.setItem('mc-artifacts-view', 'table')
    renderWithProviders(<ArtifactsPage />)
    const nav = await waitFor(() => screen.getByRole('navigation', { name: 'Artifact library' }))
    await user.click(within(nav).getByRole('button', { name: /All Artifacts/ }))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'All Artifacts' })).toBeInTheDocument())
    expect(screen.getByText('loose one')).toBeInTheDocument()
  })
})

describe('Artifacts desktop routing and filing', () => {
  it('opens All artifacts, not Home, from a folder breadcrumb root', async () => {
    const user = userEvent.setup()
    seed({ artifacts: [mkArtifact('filed-one', { folder_id: 'ops' })], folders: [mkFolder('ops', 'Ops')] })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?folder=ops' })
    const crumbs = await waitFor(() => screen.getByRole('navigation', { name: 'Folder breadcrumb' }))
    await user.click(within(crumbs).getByRole('button', { name: 'All Artifacts' }))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'All Artifacts' })).toBeInTheDocument())
    expect(screen.queryByTestId('artifacts-home')).toBeNull()
  })

  it('lists filed rows flat in the All artifacts table, so a guide can pick one in a closed folder', async () => {
    localStorage.setItem('mc-artifacts-view', 'table')
    seed({ artifacts: [mkArtifact('filed-doc', { folder_id: 'ops' })], folders: [mkFolder('ops', 'Ops')] })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?scope=all' })
    await waitFor(() => expect(document.querySelector('tr[data-guide-pick="filed doc"]')).not.toBeNull())
  })

  it('keeps unsaved chat documents out of a folder searched in the gallery', async () => {
    const user = userEvent.setup()
    seed({ artifacts: [mkArtifact('ops-plan', { folder_id: 'ops' })], folders: [mkFolder('ops', 'Ops')], docs: [doc] })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?folder=ops' })
    await waitFor(() => expect(screen.getByText('ops plan')).toBeInTheDocument())
    await user.type(screen.getByPlaceholderText(/Filter by name/), 'plan')
    await waitFor(() => expect(screen.getByText('ops plan')).toBeInTheDocument())
    expect(screen.queryByText('plan.md')).toBeNull()
  })

  it('shows a failed publish sync with its reason in Home\'s table rows, not only the red mark', async () => {
    localStorage.setItem('mc-artifacts-view', 'table')
    seed({
      artifacts: [mkArtifact('shared-doc', {
        kind: 'markdown',
        publication: {
          artifact_id: 'r1', view_url: 'https://example.com/artifact/r1', provider: 'example',
          visibility: 'SHARED', shared_with: [], auto_sync: true, last_error: 'token expired',
          last_synced_kirocrew_version: 1, version_map: { 1: 1 },
        } as unknown as Artifact['publication'],
      })],
    })
    renderWithProviders(<ArtifactsPage />)
    const recent = await waitFor(() => group('Recent'))
    await waitFor(() => expect(within(recent).getByTestId('publication-sync-error')).toBeInTheDocument())
    expect(within(recent).getByText('token expired')).toBeInTheDocument()
  })

  it('shows an unstarred saved document once, as its artifact, not again as output', async () => {
    seed({
      artifacts: [mkArtifact('plan', { name: 'plan.md', kind: 'markdown', pinned: false, sweepable: false, source_path: doc.path })],
      // The scan counts only pinned records as saved, so after an unstar it
      // reports the document unsaved again.
      docs: [doc],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(within(group('Recent')).getByText('plan.md')).toBeInTheDocument())
    expect(within(group('Recent output')).queryByText('plan.md')).toBeNull()
    expect(within(home()).getAllByText('plan.md')).toHaveLength(1)
  })

  it('keeps a starred chat document in view at the top of Recent, even behind eight newer kept records', async () => {
    const user = userEvent.setup()
    const kept = Array.from({ length: 8 }, (_, i) => mkArtifact(`kept-${i}`, { kind: 'markdown', updated_at: `2026-11-0${i + 2}T10:00:00+00:00` }))
    const server = { saved: false }
    const m = seed({ artifacts: kept, docs: [doc] })
    m.artifacts = vi.fn().mockImplementation(async () => ({
      artifacts: server.saved
        ? [...kept, mkArtifact('plan', { name: 'plan.md', kind: 'markdown', pinned: true, sweepable: false, source_path: doc.path, updated_at: '2026-10-01T10:00:00+00:00' })]
        : kept,
      output_cap: 200,
    }))
    m.artifactSessionDocs = vi.fn().mockImplementation(async () => ({ docs: server.saved ? [] : [doc] }))
    m.materializeArtifact = vi.fn().mockImplementation(async () => { server.saved = true; return { slug: 'plan' } })
    const store = createTestStore()
    store.dispatch(sseConnected())
    renderWithProviders(<ArtifactsPage />, { store })
    const output = await waitFor(() => group('Recent output'))
    await waitFor(() => expect(within(output).getByText('plan.md')).toBeInTheDocument())
    await user.click(within(output).getByRole('button', { name: /star|keep|save/i }))
    await waitFor(() => expect(m.materializeArtifact).toHaveBeenCalled())
    await waitFor(() => expect(within(group('Recent')).getByText('plan.md')).toBeInTheDocument())
  })

  it('turns Starred off when Home opens All artifacts, so See all shows what Home listed', async () => {
    const user = userEvent.setup()
    localStorage.setItem('mc-artifacts-pinned-only', '1')
    const kept = Array.from({ length: 9 }, (_, i) => mkArtifact(`kept-${i}`, { kind: 'markdown', updated_at: `2026-11-0${i + 1}T10:00:00+00:00` }))
    seed({ artifacts: kept })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(within(group('Recent')).getByText('kept 8')).toBeInTheDocument())
    await user.click(within(group('Recent')).getByRole('button', { name: 'See all' }))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'All Artifacts' })).toBeInTheDocument())
    await waitFor(() => expect(screen.getByText('kept 0')).toBeInTheDocument())
    expect(localStorage.getItem('mc-artifacts-pinned-only')).toBe('0')
  })

  it('keeps a starred output in view at the top of Recent, even behind eight newer kept records', async () => {
    const user = userEvent.setup()
    const kept = Array.from({ length: 8 }, (_, i) => mkArtifact(`kept-${i}`, { kind: 'markdown', updated_at: `2026-11-0${i + 2}T10:00:00+00:00` }))
    const out = mkArtifact('old-output', { sweepable: true, updated_at: '2026-10-01T10:00:00+00:00' })
    const server = { out: { ...out } }
    const m = seed({ artifacts: kept })
    m.artifacts = vi.fn().mockImplementation(async () => ({ artifacts: [...kept, { ...server.out }], output_cap: 200 }))
    m.setArtifactPinned = vi.fn().mockImplementation(async () => { server.out = { ...server.out, pinned: true, sweepable: false }; return {} })
    const store = createTestStore()
    store.dispatch(sseConnected())
    renderWithProviders(<ArtifactsPage />, { store })
    const output = await waitFor(() => group('Recent output'))
    await waitFor(() => expect(within(output).getByText('old output')).toBeInTheDocument())
    await user.click(within(output).getByRole('button', { name: 'Star to keep' }))
    await waitFor(() => expect(within(group('Recent')).getByText('old output')).toBeInTheDocument())
    expect(within(group('Recent output')).queryByText('old output')).toBeNull()
  })

  it('says the library is empty under All artifacts, never that everything is filed', async () => {
    seed({ folders: [mkFolder('ops', 'Ops')] })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?scope=all' })
    await waitFor(() => expect(screen.getByRole('heading', { name: 'All Artifacts' })).toBeInTheDocument())
    await waitFor(() => expect(screen.getByText('No artifacts yet')).toBeInTheDocument())
    expect(screen.queryByText(/everything is filed in folders/i)).toBeNull()
  })

  it('narrows Starred inside the selected folder rather than listing every folder', async () => {
    localStorage.setItem('mc-artifacts-pinned-only', '1')
    seed({
      artifacts: [
        mkArtifact('ops-starred', { folder_id: 'ops', pinned: true }),
        mkArtifact('other-starred', { folder_id: 'other', pinned: true }),
        mkArtifact('ops-plain', { folder_id: 'ops' }),
      ],
      folders: [mkFolder('ops', 'Ops'), mkFolder('other', 'Other')],
    })
    for (const view of ['grid', 'table']) {
      localStorage.setItem('mc-artifacts-view', view)
      renderWithProviders(<ArtifactsPage />, { route: '/artifacts?folder=ops' })
      await waitFor(() => expect(screen.getByText('ops starred')).toBeInTheDocument())
      expect(screen.queryByText('other starred')).toBeNull()
      expect(screen.queryByText('ops plain')).toBeNull()
      cleanup()
    }
  })

  it('files a new document into the selected folder while Starred is on', async () => {
    const user = userEvent.setup()
    localStorage.setItem('mc-artifacts-pinned-only', '1')
    const m = seed({ folders: [mkFolder('ops', 'Ops')] })
    m.createArtifact = vi.fn().mockResolvedValue(mkArtifact('untitled'))
    m.setArtifactFolder = vi.fn().mockResolvedValue({})
    const store = createTestStore()
    store.dispatch(sseConnected())
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?folder=ops', store })
    await user.click(await screen.findByRole('button', { name: 'Doc' }))
    await waitFor(() => expect(m.setArtifactFolder).toHaveBeenCalledWith('untitled', 'ops'))
  })

  it('names each table row for a guide pick, on Home and under All artifacts', async () => {
    localStorage.setItem('mc-artifacts-view', 'table')
    seed({ artifacts: [mkArtifact('kept-doc')] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(within(home()).getByText('kept doc')).toBeInTheDocument())
    expect(home().querySelector('[data-guide-pick="kept doc"]')).not.toBeNull()

    cleanup()
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?scope=all' })
    await waitFor(() => expect(screen.getByText('kept doc')).toBeInTheDocument())
    expect(document.querySelector('tr[data-guide-pick="kept doc"]')).not.toBeNull()
  })

  it('opens a parent folder and expands it from one control, keeping two controls a row', async () => {
    const user = userEvent.setup()
    seed({ folders: [mkFolder('ops', 'Ops'), mkFolder('sub', 'Runbooks', { parent_id: 'ops' })] })
    renderWithProviders(<ArtifactsPage />)
    const nav = await waitFor(() => screen.getByRole('navigation', { name: 'Artifact library' }))
    const ops = await waitFor(() => within(nav).getByRole('button', { name: /^Ops/ }))
    // The row's only controls are the folder button and its menu trigger.
    const row = ops.parentElement as HTMLElement
    expect(within(row).getAllByRole('button').filter(b => b !== ops && !b.getAttribute('aria-label')?.startsWith('Actions for folder'))).toHaveLength(0)
    expect(ops).toHaveAttribute('aria-expanded', 'false')
    await user.click(ops)
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Ops' })).toBeInTheDocument())
    expect(ops).toHaveAttribute('aria-expanded', 'true')
    expect(within(nav).getByRole('button', { name: /^Runbooks/ })).toBeInTheDocument()
    // Pressing the open folder again collapses it and keeps it open.
    await user.click(ops)
    expect(ops).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByRole('heading', { name: 'Ops' })).toBeInTheDocument()
  })
})

describe('moving an artifact between folders', () => {
  it('refetches the artifact lists once the move settles, so Home re-reads whether it can be swept', async () => {
    const m = seed({ artifacts: [mkArtifact('chat-widget', { sweepable: true })] })
    m.setArtifactFolder = vi.fn().mockResolvedValue({})
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const spy = vi.spyOn(qc, 'invalidateQueries')
    const store = createTestStore()
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <Provider store={store}><QueryClientProvider client={qc}>{children}</QueryClientProvider></Provider>
    )
    const { result } = renderHook(() => useMoveArtifactToFolder(), { wrapper })
    act(() => result.current('chat-widget', 'ops'))
    await waitFor(() => expect(m.setArtifactFolder).toHaveBeenCalledWith('chat-widget', 'ops'))
    await waitFor(() => expect(spy).toHaveBeenCalledWith({ queryKey: ['artifacts'] }))
  })
})
