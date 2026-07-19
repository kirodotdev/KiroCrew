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
})
