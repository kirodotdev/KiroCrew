import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { NavigationLeaveGuardProvider } from '../components/NavigationLeaveGuard'
import { emptyCanvas, serializeCanvas } from '../components/canvas/canvasModel'
import { registerCanvasPending } from '../components/canvas/canvasPending'
import { api } from '../api/client'
import type { Artifact, ArtifactComment } from '../types'
import { renderWithProviders } from './helpers'
import { namedCeiling } from './namedCeiling'

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({ default: () => <div />, PREFILL_STORAGE_KEY: 'kirocrew_prefill' }))
let mobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))

const canvas: Artifact = {
  slug: 'board', name: 'Board', kind: 'canvas', source: 'manual', description: 'Sprint plan', tags: [],
  version: 1, content_token: 't1', content: serializeCanvas(emptyCanvas()),
  created_at: '2026-11-13T12:00:00Z', updated_at: '2026-11-13T12:00:00Z',
}
const notes: Artifact = { ...canvas, slug: 'notes', name: 'Notes', kind: 'markdown', content: '# Notes', content_token: undefined }

// The board renders behind the artifact query and then its lazy chunk (lazyCanvas → lazyRetry →
// CanvasArtifactBody), several seconds under a loaded runner: every wait on that chain uses this.
const BOARD_READY = namedCeiling('BOARD_READY', 5000)

async function renderPage(artifact: Artifact, comments: ArtifactComment[] = [], versions: number[] | Error = [1]) {
  vi.mocked(api).artifact = vi.fn().mockResolvedValue(artifact)
  vi.mocked(api).artifactVersions = versions instanceof Error
    ? vi.fn().mockRejectedValue(versions)
    : vi.fn().mockResolvedValue({ slug: artifact.slug, versions })
  vi.mocked(api).artifactVersion = vi.fn().mockResolvedValue(artifact)
  vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: artifact.slug, events: [] })
  vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments })
  vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
  renderWithProviders(
    <NavigationLeaveGuardProvider>
      <Routes>
        <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
        <Route path="/artifacts" element={<div data-testid="library" />} />
      </Routes>
    </NavigationLeaveGuardProvider>,
    { route: `/artifacts/${artifact.slug}` },
  )
  await screen.findByRole('button', { name: 'More actions' }, undefined, BOARD_READY)
  await waitFor(() => expect(api.artifactEvents).toHaveBeenCalled(), BOARD_READY)
  if (artifact.kind === 'canvas') await screen.findByTestId('canvas-artifact', undefined, BOARD_READY)
}

beforeEach(() => { vi.mocked(api).artifactFolders = vi.fn().mockResolvedValue({ folders: [] }) })
afterEach(() => { cleanup(); vi.restoreAllMocks(); mobile = false })

function openMore() {
  fireEvent.keyDown(screen.getByRole('button', { name: 'More actions' }), { key: 'Enter' })
}

describe('canvas detail page', () => {
  it('fills the page edge to edge, with no header row, border or rounding', async () => {
    await renderPage(canvas)
    const box = screen.getByTestId('canvas-artifact')
    expect(box.className).toContain('absolute inset-0')
    expect(box.className).not.toMatch(/\b(border|rounded)/)
    expect(screen.getByTestId('canvas-page-pill')).toBeInTheDocument()
  })

  it('keeps the controls off the zoom control\'s corner at every width, with no width cap', async () => {
    await renderPage(canvas)
    const controls = screen.getByTestId('canvas-page-controls')
    expect(controls).toContainElement(screen.getByTestId('canvas-page-pill'))
    expect(controls).toHaveClass('absolute', 'left-3', 'right-[5.5rem]', 'top-3')
    expect(controls).not.toContainElement(screen.getByTestId('canvas-zoom'))
    const column = screen.getByTestId('canvas-page-pill').parentElement!
    expect(column).toHaveClass('max-w-full')
    expect(column.className).not.toMatch(/cqw/)
    expect(screen.getByTestId('canvas-page-board')).not.toHaveClass('@container')
  })

  it('lets the board beside the pill take clicks while a notice widens the controls column', async () => {
    vi.mocked(api).setArtifactPinned = vi.fn().mockRejectedValue(new Error('star refused'))
    await renderPage(canvas)
    openMore()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Star artifact' }))
    const notice = (await screen.findByText('star refused')).closest('[role="alert"]') as HTMLElement
    expect(screen.getByTestId('canvas-page-controls')).toHaveClass('pointer-events-none')
    expect(screen.getByTestId('canvas-page-controls-column')).toHaveClass('pointer-events-none')
    expect(screen.getByTestId('canvas-page-controls-column')).not.toHaveClass('pointer-events-auto')
    expect(screen.getByTestId('canvas-page-pill')).toHaveClass('pointer-events-auto')
    for (const stack of [screen.getByTestId('canvas-page-notices'), screen.getByTestId('canvas-page-board-notices')]) {
      expect(stack).not.toHaveClass('pointer-events-auto')
      expect(stack).toHaveClass('[&>*]:pointer-events-auto')
    }
    expect(screen.getByTestId('canvas-page-notices')).toContainElement(notice)
  })

  it('floats Back, title, version, chat and More in one pill, and leaves the rest out', async () => {
    await renderPage(canvas)
    const pill = within(screen.getByTestId('canvas-page-pill'))
    expect(pill.getByRole('button', { name: 'Back' })).toBeInTheDocument()
    expect(pill.getByRole('button', { name: 'Board' })).toBeInTheDocument()
    expect(pill.getByRole('button', { name: 'Version' })).toBeInTheDocument()
    expect(pill.queryByRole('button', { name: 'Star artifact' })).toBeNull()
    expect(pill.getByRole('button', { name: 'Toggle agent chat' })).toHaveAttribute('aria-pressed', 'false')
    expect(pill.getByRole('button', { name: 'More actions' })).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Compare with' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Add a tag' })).toBeNull()
    expect(screen.queryByText('Activity')).toBeNull()
    expect(screen.queryByText(/^Created /)).toBeNull()
  })

  it('opens the agent chat as a column beside the canvas', async () => {
    vi.mocked(api).createChatSlot = vi.fn().mockResolvedValue({ key: 'chat-new' })
    await renderPage(canvas)
    expect(screen.queryByTestId('canvas-page-chat-panel')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Toggle agent chat' }))
    expect(await screen.findByTestId('canvas-page-chat-panel')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Toggle agent chat' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByTestId('canvas-artifact')).toBeInTheDocument()
  })

  it('stars from the More menu', async () => {
    vi.mocked(api).setArtifactPinned = vi.fn().mockResolvedValue({})
    await renderPage(canvas)
    openMore()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Star artifact' }))
    await waitFor(() => expect(api.setArtifactPinned).toHaveBeenCalledWith('board', true))
  })

  it('opens the Publish panel from the More menu as the side column, and closes it', async () => {
    vi.mocked(api).publishProviders = vi.fn().mockResolvedValue({ providers: [] })
    vi.mocked(api).getArtifactPublishProviders = vi.fn().mockResolvedValue({ providers: [], kind: 'canvas' })
    await renderPage(canvas)
    openMore()
    fireEvent.click(await screen.findByRole('menuitemcheckbox', { name: 'Publish' }))
    const column = await screen.findByTestId('canvas-page-publish')
    expect(await within(column).findByText(/No publish providers available/)).toBeInTheDocument()
    expect(api.getArtifactPublishProviders).toHaveBeenCalledWith('canvas')
    expect(screen.getByTestId('canvas-artifact')).toBeInTheDocument()
    fireEvent.click(within(column).getByRole('button', { name: 'Close publish panel' }))
    await waitFor(() => expect(screen.queryByTestId('canvas-page-publish')).toBeNull())
  })

  it('offers Pop out from the More menu', async () => {
    await renderPage(canvas)
    openMore()
    expect(await screen.findByRole('menuitem', { name: 'Pop out to window' })).toBeInTheDocument()
  })

  it('shows a failed star under the pill', async () => {
    vi.mocked(api).setArtifactPinned = vi.fn().mockRejectedValue(new Error('star refused'))
    await renderPage(canvas)
    openMore()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Star artifact' }))
    const notice = (await screen.findByText('star refused')).closest('[role="alert"]') as HTMLElement
    expect(notice).toHaveTextContent(/Save failed/)
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
  })

  it('shows a versions-load failure under the pill without a draft-losing hand-off', async () => {
    await renderPage(canvas, [], new Error('versions unavailable'))
    const notice = (await screen.findByText('versions unavailable')).closest('[role="alert"]') as HTMLElement
    expect(notice).toHaveTextContent('Versions failed to load')
    const pill = screen.getByTestId('canvas-page-pill')
    expect(pill.parentElement).toContainElement(notice)
    expect(pill).not.toContainElement(notice)
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
  })

  // A canvas can be published with auto-sync, so a failed push or an upstream move must
  // show here as on the generic page, which this page replaces for a canvas.
  const publication = {
    artifact_id: 'pub-1', view_url: 'https://remote.example.com/artifact/pub-1', provider: 'companion',
    visibility: 'SHARED' as const, shared_with: [], auto_sync: true, last_synced_kirocrew_version: 1,
    version_map: {}, published_at: '2026-07-01T00:00:00Z', published_by: 'joe', last_error: '',
  }

  it('shows a publication sync failure under the pill without a draft-losing hand-off', async () => {
    await renderPage({ ...canvas, publication: { ...publication, last_error: 'push refused' } })
    const notice = (await screen.findByText('push refused')).closest('[role="alert"]') as HTMLElement
    expect(notice).toHaveTextContent('Publication sync issue')
    const pill = screen.getByTestId('canvas-page-pill')
    expect(pill.parentElement).toContainElement(notice)
    expect(pill).not.toContainElement(notice)
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
  })

  it('shows the upstream sync banner under the pill when the remote copy moved', async () => {
    vi.mocked(api).getArtifactPublishProviders = vi.fn().mockResolvedValue({
      providers: [{ name: 'companion', display_name: 'Companion' }], kind: 'canvas',
    })
    vi.mocked(api).upstreamStatus = vi.fn().mockResolvedValue({ upstream_ahead: true })
    await renderPage({ ...canvas, publication })
    const pull = await screen.findByTitle(/Pull the latest remote content/i, undefined, BOARD_READY)
    const pill = screen.getByTestId('canvas-page-pill')
    expect(pill.parentElement).toContainElement(pull)
    expect(pill).not.toContainElement(pull)
    expect(screen.getByTitle(/Push your local version up/i)).toBeInTheDocument()
  })

  it.each([
    { action: 'pull', title: /Pull the latest remote content/i, call: 'pullLatest', ahead: true },
    { action: 'overwrite', title: /Push your local version up/i, call: 'overwriteRemote', ahead: true },
    { action: 'snapshot', title: /Snapshot the current content as a new version/i, call: 'updateArtifact', ahead: false },
  ] as const)('stops the banner\'s $action when the board\'s waiting autosave fails', async ({ title, call, ahead }) => {
    vi.mocked(api).getArtifactPublishProviders = vi.fn().mockResolvedValue({
      providers: [{ name: 'companion', display_name: 'Companion' }], kind: 'canvas',
    })
    vi.mocked(api).upstreamStatus = vi.fn().mockResolvedValue({ upstream_ahead: ahead, live_dirty: !ahead })
    vi.mocked(api).pullLatest = vi.fn().mockResolvedValue({})
    vi.mocked(api).overwriteRemote = vi.fn().mockResolvedValue({})
    vi.mocked(api).updateArtifact = vi.fn().mockResolvedValue({})
    await renderPage({ ...canvas, publication })
    const button = await screen.findByTitle(title, undefined, BOARD_READY)
    // The board's autosave could not land (a conflict or a refused save).
    const unregister = registerCanvasPending('board', {
      flush: () => Promise.resolve(false), discard: () => {}, document: () => null,
    })
    try {
      fireEvent.click(button)
      const notice = await screen.findByText('The canvas could not be saved', { selector: '[role="alert"] *' })
      expect(notice).toBeInTheDocument()
      expect(api[call]).not.toHaveBeenCalled()
    } finally {
      unregister()
    }
  })

  it.each(['Activity', 'Hide comments', 'Collapse comments', 'Toggle agent chat', 'Draft a prompt in chat'])(
    'keeps an unsent comment mounted when choosing %s, and restores it on return', async target => {
      vi.mocked(api).createChatSlot = vi.fn().mockResolvedValue({ key: 'chat-new' })
      await renderPage(canvas, [{
        id: 'c1', origin: 'local', scope: 'private', author: 'me', is_agent: false, body: 'nice board',
        thread_id: 'c1', status: 'open', sync_state: 'local', created_at: '2026-11-13T12:00:00Z',
      } as ArtifactComment])
      openMore()
      fireEvent.click(await screen.findByRole('menuitem', { name: 'Show comments' }))
      const comments = await screen.findByTestId('canvas-page-comments')
      fireEvent.click(within(comments).getByRole('button', { name: 'Add comment' }))
      const draft = within(comments).getByRole('textbox', { name: 'Add comment' })
      fireEvent.change(draft, { target: { value: 'Keep this unfinished thought' } })

      if (target === 'Activity' || target === 'Hide comments') {
        openMore()
        fireEvent.click(await screen.findByRole('menuitem', { name: target }))
      } else {
        fireEvent.click(screen.getByRole('button', { name: target, exact: true }))
      }
      if (target === 'Activity') await screen.findByTestId('canvas-page-activity')
      if (target === 'Toggle agent chat' || target === 'Draft a prompt in chat') {
        await screen.findByTestId('canvas-page-chat-panel')
      }
      expect(draft).toBeInTheDocument()
      expect(comments).toHaveClass('hidden')
      expect(screen.queryByRole('textbox', { name: 'Add comment' })).toBeNull()
      expect(draft).toHaveValue('Keep this unfinished thought')
      openMore()
      fireEvent.click(await screen.findByRole('menuitem', { name: 'Show comments' }))
      expect(screen.getByRole('textbox', { name: 'Add comment' })).toBe(draft)
      expect(draft).toHaveValue('Keep this unfinished thought')
    },
  )

  it.each(['Reply', 'Edit'])('keeps an unsent %s draft when switching to Activity', async action => {
    await renderPage(canvas, [{
      id: 'c1', origin: 'local', scope: 'private', author: 'me', is_agent: false, body: 'nice board',
      thread_id: 'c1', status: 'open', sync_state: 'local', created_at: '2026-11-13T12:00:00Z',
    } as ArtifactComment])
    openMore()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Show comments' }))
    const comments = await screen.findByTestId('canvas-page-comments')
    fireEvent.click(within(comments).getByRole('button', { name: action, exact: true }))
    const draft = within(comments).getByRole('textbox')
    fireEvent.change(draft, { target: { value: 'Unfinished revision' } })
    openMore()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Activity' }))
    await screen.findByTestId('canvas-page-activity')
    expect(draft).toBeInTheDocument()
    expect(comments).toHaveClass('hidden')
    expect(screen.queryByRole('textbox')).toBeNull()
    openMore()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Show comments' }))
    expect(within(comments).getByRole('textbox')).toBe(draft)
    expect(draft).toHaveValue('Unfinished revision')
  })

  it('on a phone, an open chat takes the whole width and the board steps aside', async () => {
    mobile = true
    vi.mocked(api).createChatSlot = vi.fn().mockResolvedValue({ key: 'chat-new' })
    await renderPage(canvas)
    expect(screen.getByTestId('canvas-page-board')).not.toHaveClass('hidden')
    fireEvent.click(screen.getByRole('button', { name: 'Toggle agent chat' }))
    await screen.findByTestId('canvas-page-chat-panel')
    const column = screen.getByTestId('canvas-page-column')
    expect(column).toHaveClass('flex-1')
    expect(column).not.toHaveClass('shrink-0')
    expect(screen.getByTestId('canvas-page-board')).toHaveClass('hidden')
    expect(screen.getByTestId('canvas-artifact')).toBeInTheDocument()
  })

  it('opens comments from the More menu as the right-hand column, in place of the chat', async () => {
    vi.mocked(api).createChatSlot = vi.fn().mockResolvedValue({ key: 'chat-new' })
    await renderPage(canvas)
    fireEvent.click(screen.getByRole('button', { name: 'Toggle agent chat' }))
    await screen.findByTestId('canvas-page-chat-panel')
    openMore()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Show comments' }))
    expect(await screen.findByTestId('canvas-page-comments')).toBeInTheDocument()
    expect(screen.queryByTestId('canvas-page-chat-panel')).toBeNull()
    expect(screen.getByTestId('canvas-artifact')).toBeInTheDocument()
    openMore()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Hide comments' }))
    await waitFor(() => expect(screen.queryByTestId('canvas-page-comments')).toBeNull())
  })

  it('does not open the comments column by itself when the canvas has comments', async () => {
    await renderPage(canvas, [{
      id: 'c1', origin: 'local', scope: 'private', author: 'me', is_agent: false, body: 'nice board',
      thread_id: 'c1', status: 'open', sync_state: 'local', created_at: '2026-11-13T12:00:00Z',
    } as ArtifactComment])
    await waitFor(() => expect(api.artifactComments).toHaveBeenCalled())
    expect(screen.queryByTestId('canvas-page-comments')).toBeNull()
    expect(screen.queryByTestId('canvas-page-column')).toBeNull()
  })

  it('shows the activity log from the More menu, and closes it', async () => {
    await renderPage(canvas)
    openMore()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Activity' }))
    const activity = await screen.findByTestId('canvas-page-activity')
    expect(within(activity).getByText('Activity')).toBeInTheDocument()
    fireEvent.click(within(activity).getByRole('button', { name: 'Close' }))
    expect(screen.queryByTestId('canvas-page-activity')).toBeNull()
  })

  it('edits tags in a popover under the pill', async () => {
    await renderPage({ ...canvas, tags: ['planning'] })
    expect(screen.queryByText('planning')).toBeNull()
    openMore()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Tags' }))
    const tags = within(await screen.findByTestId('canvas-page-tags'))
    expect(tags.getByText('planning')).toBeInTheDocument()
    expect(tags.getByRole('button', { name: 'Add a tag' })).toBeInTheDocument()
  })

  it('offers Revert from the More menu on a past version only, and reverts after the question', async () => {
    const live = { ...canvas, version: 2 }
    await renderPage(live, [], [1, 2])
    vi.mocked(api).artifactVersion = vi.fn().mockResolvedValue({ ...canvas, version: 1, content: 'v1 board' })
    vi.mocked(api).updateArtifact = vi.fn().mockResolvedValue({ ...live, version: 3 })
    openMore()
    await screen.findByRole('menuitem', { name: 'Show comments' })
    expect(screen.queryByRole('menuitem', { name: /^Revert/ })).toBeNull()
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())

    fireEvent.keyDown(screen.getByRole('button', { name: 'Version' }), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('menuitemradio', { name: 'v1' }))
    await waitFor(() => expect(api.artifactVersion).toHaveBeenCalledWith('board', 1))
    // The past version's record replaces the live one; wait for the switch before using the menu.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Version' })).toHaveTextContent('v1'), BOARD_READY)
    openMore()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Revert to v1' }))
    const question = await screen.findByRole('dialog', { name: 'Revert' })
    expect(api.updateArtifact).not.toHaveBeenCalled()
    fireEvent.click(within(question).getByRole('button', { name: 'Revert' }))
    await waitFor(() => expect(api.updateArtifact).toHaveBeenCalledWith('board', {
      content: 'v1 board', event_type: 'reverted', from_version: 1,
    }))
  })

  it('leaves through Back', async () => {
    await renderPage(canvas)
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(await screen.findByTestId('library')).toBeInTheDocument()
  })

  it('opens an empty canvas on the same page', async () => {
    await renderPage(canvas)
    expect(screen.getByTestId('canvas-editor')).toBeInTheDocument()
    expect(screen.queryAllByTestId('canvas-shape')).toHaveLength(0)
  })
})

describe('a page of any other kind', () => {
  it('keeps the generic header, its chips and its footer', async () => {
    await renderPage(notes)
    expect(screen.queryByTestId('canvas-page-pill')).toBeNull()
    expect(screen.getByRole('button', { name: 'Star artifact' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add a tag' })).toBeInTheDocument()
    expect(screen.getByText(/Created /)).toBeInTheDocument()
    expect(screen.getByText('Activity')).toBeInTheDocument()
  })
})
