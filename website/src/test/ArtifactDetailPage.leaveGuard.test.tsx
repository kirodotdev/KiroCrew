/**
 * An unsaved edit buffer answers the app shell's leave check.
 *
 * Exits the page does not own — the global sidebar, the command palette, and a
 * popout's forwarded navigation intent — ask `useMayLeaveNow` before
 * they swap the route. A client-side route change never fires `beforeunload`,
 * so without a registered guard those exits discarded the edit silently. The
 * registry needs a synchronous answer, so a dirty buffer refuses and opens the
 * app's async discard dialog instead of `window.confirm`.
 *
 * `ContentRenderer` is mocked to a textarea for the same reason as in
 * ArtifactDetailPage.dirtyDelete.test.tsx: Monaco renders no input under jsdom.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, act, within } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { NavigationLeaveGuardProvider } from '../components/NavigationLeaveGuard'
import { useMayLeaveAsPromise } from './useMayLeaveAsPromise'
import { renderWithProviders } from './helpers'
import { openMore, closeMore } from './artifactMoreMenu'
import { api } from '../api/client'
import { __resetArtifactEditing } from '../utils/artifactEditGuard'
import type { Artifact } from '../types'

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))
vi.mock('../components/ContentRenderer', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../components/ContentRenderer')>()),
  ContentRenderer: ({ editing, displayContent, onChange }: {
    editing: boolean; displayContent: string; onChange: (v: string) => void
  }) => editing
    ? <textarea aria-label="editor" defaultValue={displayContent} onChange={e => onChange(e.target.value)} />
    : <div>{displayContent}</div>,
}))

const mkArtifact = (o: Partial<Artifact> = {}): Artifact => ({
  slug: 'cr-queue', name: 'CR Queue', kind: 'markdown', source: 'chat', description: '',
  tags: [], version: 1, created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:30:00.000000+00:00', content: '# v1', ...o,
})

let ask: () => Promise<boolean> = async () => true
function ShellProbe() {
  ask = useMayLeaveAsPromise()
  return null
}

function renderPage() {
  return renderWithProviders(
    <NavigationLeaveGuardProvider>
      <ShellProbe />
      <Routes>
        <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
      </Routes>
    </NavigationLeaveGuardProvider>,
    { route: '/artifacts/cr-queue' },
  )
}

async function enterEditMode() {
  renderPage()
  await waitFor(() => expect(screen.getByLabelText('Toggle agent chat')).toBeInTheDocument())
  fireEvent.click(screen.getByRole('button', { description: /edit content/i }))
  return screen.findByLabelText('editor')
}

describe('ArtifactDetailPage app-shell leave guard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    __resetArtifactEditing()
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'cr-queue', versions: [1] })
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'cr-queue', events: [] })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('asks the app discard dialog over a dirty buffer and leaves only on discard', async () => {
    const nativeConfirm = vi.spyOn(window, 'confirm')
    const editor = await enterEditMode()
    fireEvent.change(editor, { target: { value: '# unsaved work' } })
    let answer: Promise<boolean> = Promise.resolve(true)
    act(() => { answer = ask() })
    expect(nativeConfirm).not.toHaveBeenCalled()
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/discard unsaved changes/i)).toBeInTheDocument()
    expect(screen.getAllByRole('dialog')).toHaveLength(1)
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: /discard changes/i }))
      await answer
    })
    await expect(answer).resolves.toBe(true)
    await waitFor(() => expect(screen.queryByLabelText('editor')).toBeNull())
    // The buffer is gone, so a later navigation leaves without asking.
    let again: Promise<boolean> = Promise.resolve(false)
    await act(async () => { again = ask(); await again })
    await expect(again).resolves.toBe(true)
  })

  it('keeps the edit and refuses the exit when Keep editing is chosen', async () => {
    const editor = await enterEditMode()
    fireEvent.change(editor, { target: { value: '# unsaved work' } })
    let answer: Promise<boolean> = Promise.resolve(true)
    act(() => { answer = ask() })
    const dialog = await screen.findByRole('dialog')
    // A leave guard names what staying means instead of the generic Cancel.
    expect(within(dialog).queryByRole('button', { name: 'Cancel' })).toBeNull()
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Keep editing' }))
      await answer
    })
    await expect(answer).resolves.toBe(false)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect((screen.getByLabelText('editor') as HTMLTextAreaElement).value).toBe('# unsaved work')
  })

  it('lets the shell leave without asking while the buffer is clean', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm')
    await enterEditMode()
    let answer: Promise<boolean> = Promise.resolve(false)
    await act(async () => { answer = ask(); await answer })
    await expect(answer).resolves.toBe(true)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(confirmSpy).not.toHaveBeenCalled()
  })

  it('a save still in flight at the discard shows no failure notice afterwards', async () => {
    let failSave: (err: Error) => void = () => {}
    vi.mocked(api).updateArtifact = vi.fn().mockImplementation(
      () => new Promise((_resolve, reject) => { failSave = reject }),
    )
    const editor = await enterEditMode()
    fireEvent.change(editor, { target: { value: '# unsaved work' } })
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    await waitFor(() => expect(api.updateArtifact).toHaveBeenCalledTimes(1))
    act(() => { void ask() })
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /discard changes/i }))
    await waitFor(() => expect(screen.queryByLabelText('editor')).toBeNull())
    await act(async () => { failSave(new Error('disk is full')) })
    // The write fails after the buffer is gone: nothing is left to save, so
    // the view page must not report a save failure.
    await new Promise(r => setTimeout(r, 0))
    expect(screen.queryByText('disk is full')).toBeNull()
  })
})

describe('ArtifactDetailPage Escape inside the More menu', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    __resetArtifactEditing()
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'cr-queue', versions: [1] })
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'cr-queue', events: [] })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('closes the menu and keeps a clean editor open', async () => {
    await enterEditMode()
    openMore()
    await screen.findByRole('menu')
    await closeMore()
    expect(screen.getByLabelText('editor')).toBeInTheDocument()
  })

  it('closes the menu without asking to discard a dirty buffer', async () => {
    const editor = await enterEditMode()
    fireEvent.change(editor, { target: { value: '# unsaved work' } })
    openMore()
    await screen.findByRole('menu')
    await closeMore()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect((screen.getByLabelText('editor') as HTMLTextAreaElement).value).toBe('# unsaved work')
  })

  it('still cancels the edit on an Escape outside the menu', async () => {
    await enterEditMode()
    fireEvent.keyDown(document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByLabelText('editor')).toBeNull())
  })
})
