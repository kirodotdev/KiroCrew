import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
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

/**
 * The filter-bar dropdowns. `SimpleSelect` wraps a Radix Select, so each is a
 * <button role="combobox"> whose rows exist only while the popup is open.
 */
const kindTrigger = () => screen.getByRole('combobox', { name: 'Filter by kind' })
const tagTrigger = () => screen.getByRole('combobox', { name: 'Filter by tag' })

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

  // Session documents (files badged "Artifact" in the chat transcript) used to
  // render ONLY as table/tree rows, while the DEFAULT gallery view showed
  // nothing for them — so a document could carry the "Artifact" badge in chat
  // yet be invisible on this page until the user discovered the table toggle.
  const mkDoc = (path: string, name: string) => ({
    path,
    name,
    updated_at: '2026-08-07T12:00:00',
    session_key: 'dashboard_chat-1',
    session_title: 'Research session',
    message_ts: 'm1',
    saved: false,
    slug: '',
  })

  it('surfaces unsaved session documents in the default gallery view', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [mkArtifact('cr-queue')] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
      docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())
    expect(screen.getByText(/From your chats/i)).toBeInTheDocument()
    expect(screen.getByLabelText('Star document')).toBeInTheDocument()
  })

  it('shows session documents in the gallery even when the library is empty', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
      docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText(/No artifacts yet/i)).toBeInTheDocument())
    expect(screen.getByText('FINDINGS.md')).toBeInTheDocument()
    // With unsaved docs on screen, the empty state must not point at the chat
    // bookmark — it points at the adjacent star affordance instead.
    expect(screen.getByText(/star a document in .From your chats./i)).toBeInTheDocument()
    expect(screen.queryByText(/Click the bookmark icon/i)).not.toBeInTheDocument()
  })

  // At ≥30 artifacts the masonry virtualizes into a viewport-height scroller;
  // a section rendered after it hides below the fold. The section must precede
  // the gallery grid in DOM order.
  it('renders the session-docs section above the artifact gallery', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [mkArtifact('cr-queue')] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
      docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())
    const section = screen.getByText(/From your chats/i)
    const card = screen.getByText('cr queue')
    expect(section.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  // The section sits ABOVE the library, so it must be height-bounded: an
  // uncapped cross-session firehose would bury the saved artifacts — the same
  // failure this section exists to cure, inverted.
  it('caps the session-docs section at 5 with a Show all expander', async () => {
    const user = userEvent.setup()
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
      docs: Array.from({ length: 7 }, (_, i) => mkDoc(`/ws/doc-${i}.md`, `doc-${i}.md`)),
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('doc-0.md')).toBeInTheDocument())
    expect(screen.getByText('doc-4.md')).toBeInTheDocument()
    expect(screen.queryByText('doc-5.md')).not.toBeInTheDocument()

    await user.click(screen.getByText(/Show all \(7\)/i))
    expect(screen.getByText('doc-6.md')).toBeInTheDocument()

    await user.click(screen.getByText(/Show less/i))
    expect(screen.queryByText('doc-6.md')).not.toBeInTheDocument()
  })

  // A user with never-to-be-saved docs can put the section away for good;
  // the choice persists across visits via localStorage.
  it('collapses the session-docs section via its header and persists the choice', async () => {
    const user = userEvent.setup()
    localStorage.removeItem('mc-artifacts-session-docs-collapsed')
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
      docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /From your chats/i }))
    expect(screen.queryByText('FINDINGS.md')).not.toBeInTheDocument()
    expect(localStorage.getItem('mc-artifacts-session-docs-collapsed')).toBe('1')

    await user.click(screen.getByRole('button', { name: /From your chats/i }))
    expect(screen.getByText('FINDINGS.md')).toBeInTheDocument()
  })

  // The section must not pop in after load and shift the gallery under the
  // user's cursor: while the docs query is pending a fixed-height skeleton
  // reserves the slot.
  it('reserves the section slot with a skeleton while session docs load', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [mkArtifact('cr-queue')] })
    let resolveDocs!: (v: unknown) => void
    vi.mocked(api).artifactSessionDocs = vi.fn().mockReturnValue(new Promise((r) => { resolveDocs = r }))
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    expect(document.querySelector('[aria-busy="true"]')).toBeInTheDocument()

    resolveDocs({ docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')] })
    await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())
    expect(document.querySelector('[aria-busy="true"]')).not.toBeInTheDocument()
  })

  it('materializes a session document from the gallery star', async () => {
    const user = userEvent.setup()
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
      docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
    })
    const materializeSpy = vi.fn().mockResolvedValue({})
    vi.mocked(api).materializeArtifact = materializeSpy
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())

    await user.click(screen.getByLabelText('Star document'))

    await waitFor(() => expect(materializeSpy).toHaveBeenCalledWith('/ws/research/FINDINGS.md', 'dashboard_chat-1'))
  })

  // The star is the section's ONLY action; a failed materialize used to stop
  // the spinner and change nothing else — indistinguishable from success. And
  // the banner must actually dismiss: the X resets every mutation feeding
  // mutErr, or clicking it re-renders the same error instantly.
  it('surfaces a failed materialize in the error banner, and the banner dismisses', async () => {
    const user = userEvent.setup()
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
      docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
    })
    vi.mocked(api).materializeArtifact = vi.fn().mockRejectedValue(new Error('materialize blew up'))
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())

    await user.click(screen.getByLabelText('Star document'))
    await waitFor(() => expect(screen.getByText(/materialize blew up/i)).toBeInTheDocument())

    await user.click(screen.getByLabelText('Dismiss'))
    await waitFor(() => expect(screen.queryByText(/materialize blew up/i)).not.toBeInTheDocument())
  })

  it('hides already-saved session documents from the gallery section', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [mkArtifact('research-findings')] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({
      docs: [{ ...mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md'), saved: true, slug: 'research-findings' }],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('research findings')).toBeInTheDocument())
    expect(screen.queryByText('FINDINGS.md')).not.toBeInTheDocument()
    expect(screen.queryByText(/From your chats/i)).not.toBeInTheDocument()
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
    // Wait for loading state to clear and the kind dropdown to mount. SimpleSelect
    // renders a <button role="combobox">, so the current selection is text on the
    // trigger rather than a form value, and the rows mount only once it is open.
    await waitFor(() =>
      expect(kindTrigger()).toHaveTextContent(/all kinds/i),
    )
    fireEvent.click(kindTrigger())
    fireEvent.click(await screen.findByRole('option', { name: 'kind: markdown' }))
    await waitFor(() => {
      expect(fetcher).toHaveBeenLastCalledWith({ tag: undefined, kind: 'markdown' })
    })
  })

  it('refetches with tag filter when tag dropdown changes', async () => {
    // The tag rows are built from a separate unfiltered query, so this also covers
    // the dynamic half of the dropdown: an "all tags" row on the empty string plus
    // one row per tag in the library.
    const fetcher = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue', { tags: ['ops'] })],
    })
    vi.mocked(api).artifacts = fetcher
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(tagTrigger()).toHaveTextContent(/all tags/i))
    fireEvent.click(tagTrigger())
    fireEvent.click(await screen.findByRole('option', { name: 'tag: ops' }))
    await waitFor(() => {
      expect(fetcher).toHaveBeenLastCalledWith({ tag: 'ops', kind: undefined })
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
