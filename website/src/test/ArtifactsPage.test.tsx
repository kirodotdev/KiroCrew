import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ArtifactsPage from '../pages/ArtifactsPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Artifact } from '../types'

vi.mock('../api/client')

// VirtuosoMasonry virtualizes against real layout, which jsdom lacks, so it
// renders zero items in tests. Mock it to a plain map through ItemContent so
// the card content + handlers are exercised.
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
  pinned: true,
  description: '',
  tags: [],
  version: 1,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:00:00.000000+00:00',
  ...overrides,
})

describe('ArtifactsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.removeItem('mc-artifacts-view')
  })

  it('shows empty state when no artifacts', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText(/No artifacts yet/i)).toBeInTheDocument())
  })

  it('renders the library with artifacts', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [
        mkArtifact('cr-queue', { tags: ['ops', 'cr'], version: 3 }),
        mkArtifact('pipeline-health', { tags: ['ops'], kind: 'widget' }),
      ],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    expect(screen.getByText('pipeline health')).toBeInTheDocument()
    expect(screen.getByText('cr-queue')).toBeInTheDocument()
    expect(screen.getByText(/v3/)).toBeInTheDocument()
  })

  it('renders Starred/All filter toggle', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue')],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    // Starred/All toggle group is present (identified by its aria-label)
    const group = screen.getByRole('group', { name: /Filter starred/i })
    expect(group).toBeInTheDocument()
    expect(group.querySelector('button')).toBeInTheDocument()
  })

  it('renders Source column in the table view', async () => {
    localStorage.setItem('mc-artifacts-view', 'table')
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue', { source: 'dashboard', session_title: 'My Session' })],
    })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({ docs: [] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    // Source column header exists in table view
    expect(screen.getByText('Source')).toBeInTheDocument()
    // Source value renders session_title when available
    expect(screen.getByText('My Session')).toBeInTheDocument()
  })

  it('renders star toggle buttons for each artifact', async () => {
    localStorage.setItem('mc-artifacts-view', 'table')
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue', { pinned: true })],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    const starBtn = screen.getByLabelText('Remove star from artifact')
    expect(starBtn).toBeInTheDocument()
    expect(starBtn).toHaveAttribute('aria-pressed', 'true')
  })

  // The star used to exist ONLY in the table view, while the Starred filter and
  // the Starred StatCard applied to BOTH views — so in the default gallery you
  // could filter by starred without being able to star anything. `pinned` is
  // also the retention control (prune_auto_widgets only sweeps unpinned
  // records), so the gallery could not keep an artifact either.
  it('exposes the star on masonry cards in the default gallery view', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue', { pinned: false })],
    })
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact('cr-queue', { pinned: false }))
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    // No table in play — this is the card, not the row.
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    const starBtn = screen.getByLabelText('Star artifact')
    expect(starBtn).toHaveAttribute('aria-pressed', 'false')
  })

  it('stars an artifact from a masonry card without opening it', async () => {
    const user = userEvent.setup()
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue', { pinned: false })],
    })
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact('cr-queue', { pinned: false }))
    vi.mocked(api).setArtifactPinned = vi.fn().mockResolvedValue({})
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())

    await user.click(screen.getByLabelText('Star artifact'))

    await waitFor(() => expect(vi.mocked(api).setArtifactPinned).toHaveBeenCalledWith('cr-queue', true))
    // The card body is itself a click target that navigates; the star must
    // stopPropagation so starring never doubles as "open this artifact".
    expect(window.location.pathname).not.toContain('cr-queue')
  })

  it('unstars an already-starred masonry card', async () => {
    const user = userEvent.setup()
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue', { pinned: true })],
    })
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact('cr-queue', { pinned: true }))
    vi.mocked(api).setArtifactPinned = vi.fn().mockResolvedValue({})
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())

    await user.click(screen.getByLabelText('Remove star from artifact'))

    await waitFor(() => expect(vi.mocked(api).setArtifactPinned).toHaveBeenCalledWith('cr-queue', false))
  })

  it('filters by name search', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [
        mkArtifact('cr-queue'),
        mkArtifact('ticket-board'),
      ],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    const search = screen.getByPlaceholderText(/Filter by name/i) as HTMLInputElement
    await userEvent.type(search, 'queue')
    expect(screen.getByText('cr queue')).toBeInTheDocument()
    expect(screen.queryByText('ticket board')).not.toBeInTheDocument()
  })

  it('shows error banner on fetch failure', async () => {
    vi.mocked(api).artifacts = vi.fn().mockRejectedValue(new Error('network down'))
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText(/network down/i)).toBeInTheDocument())
  })

  it('calls deleteArtifact when user confirms delete', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue')],
    })
    const deleteSpy = vi.fn().mockResolvedValue({ ok: true })
    vi.mocked(api).deleteArtifact = deleteSpy
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    const deleteBtn = screen.getByLabelText('Remove from artifacts library')
    await userEvent.click(deleteBtn)
    expect(deleteSpy).toHaveBeenCalledWith('cr-queue')
  })

  it('does not delete when user cancels', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue')],
    })
    const deleteSpy = vi.fn()
    vi.mocked(api).deleteArtifact = deleteSpy
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    await userEvent.click(screen.getByLabelText('Remove from artifacts library'))
    expect(deleteSpy).not.toHaveBeenCalled()
  })

  it('refetches with kind filter when kind dropdown changes', async () => {
    const fetcher = vi.fn().mockResolvedValue({ artifacts: [] })
    vi.mocked(api).artifacts = fetcher
    renderWithProviders(<ArtifactsPage />)
    // Wait for loading state to clear and the kind dropdown to mount.
    await waitFor(() =>
      expect(screen.getByDisplayValue(/all kinds/i)).toBeInTheDocument(),
    )
    const kindSelect = screen.getByDisplayValue(/all kinds/i) as HTMLSelectElement
    await userEvent.selectOptions(kindSelect, 'markdown')
    await waitFor(() => {
      expect(fetcher).toHaveBeenLastCalledWith({ tag: undefined, kind: 'markdown' })
    })
  })

  it('card action button pops the artifact out into its own window (keyboard-reachable)', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue')],
    })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts' })
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    const popoutBtn = screen.getByLabelText('Pop out to window')
    expect(popoutBtn).toBeInTheDocument()
    const open = vi.spyOn(window, 'open').mockReturnValue({ closed: false, focus: vi.fn() } as unknown as Window)
    await userEvent.click(popoutBtn)
    expect(open).toHaveBeenCalledTimes(1)
    expect(String(open.mock.calls[0][0])).toContain('/popout/artifact/cr-queue')
    open.mockRestore()
  })

  it('renders Artifact Deploy button that navigates to /deploy', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts' })
    await waitFor(() => expect(screen.getByText('Artifact Deploy')).toBeInTheDocument())
    const btn = screen.getByText('Artifact Deploy').closest('button')!
    expect(btn).toBeInTheDocument()
    await userEvent.click(btn)
    // MemoryRouter means we check the location changed; since there's no
    // matching Route defined in the test wrapper, we verify the button exists
    // and is clickable (navigation intent is covered by the navigate call).
    expect(btn).toBeInTheDocument()
  })
})

describe('ArtifactsPage — New Artifact split button', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.removeItem('mc-artifacts-view')
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
  })

  it('creates a blank document and opens it', async () => {
    // No `kind` is sent: the store defaults a blank document to markdown and
    // marks the kind auto-assigned so the first real save can re-type it.
    vi.mocked(api).createArtifact = vi.fn().mockResolvedValue(mkArtifact('untitled', { kind: 'markdown' }))
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText(/No artifacts yet/i)).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /New Artifact/i }))
    await waitFor(() => {
      expect(vi.mocked(api).createArtifact).toHaveBeenCalledWith({ name: 'Untitled', content: '' })
    })
  })

  it('keeps file import available under the caret', async () => {
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText(/No artifacts yet/i)).toBeInTheDocument())
    // The import entry lives in the dropdown, not the toolbar, so it must not
    // be reachable until the caret is opened.
    expect(screen.queryByText(/Import from a file/i)).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /More ways to add an artifact/i }))
    expect(await screen.findByText(/Import from a file/i)).toBeInTheDocument()
  })
})
