/**
 * An unsaved edit buffer answers the app shell's leave check.
 *
 * Exits the page does not own — the global sidebar, the command palette, and a
 * popout's forwarded navigation intent — ask `useMayLeaveForNavigation` before
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
import { NavigationLeaveGuardProvider, useMayLeaveForNavigation } from '../components/NavigationLeaveGuard'
import { renderWithProviders } from './helpers'
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

let ask: () => boolean = () => true
function ShellProbe() {
  ask = useMayLeaveForNavigation()
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

  it('refuses a shell exit over a dirty buffer and offers the app discard dialog', async () => {
    const nativeConfirm = vi.spyOn(window, 'confirm')
    const editor = await enterEditMode()
    fireEvent.change(editor, { target: { value: '# unsaved work' } })
    let answer = true
    act(() => { answer = ask() })
    expect(answer).toBe(false)
    expect(nativeConfirm).not.toHaveBeenCalled()
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/discard unsaved changes/i)).toBeInTheDocument()
    // Asking again while the dialog is open still refuses and shows one dialog.
    act(() => { answer = ask() })
    expect(answer).toBe(false)
    expect(screen.getAllByRole('dialog')).toHaveLength(1)
    fireEvent.click(within(dialog).getByRole('button', { name: /discard changes/i }))
    await waitFor(() => expect(screen.queryByLabelText('editor')).toBeNull())
    // The buffer is gone, so the repeated navigation leaves without asking.
    expect(ask()).toBe(true)
  })

  it('keeps the edit when the discard dialog is cancelled', async () => {
    const editor = await enterEditMode()
    fireEvent.change(editor, { target: { value: '# unsaved work' } })
    act(() => { ask() })
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect((screen.getByLabelText('editor') as HTMLTextAreaElement).value).toBe('# unsaved work')
    expect(ask()).toBe(false)
  })

  it('lets the shell leave without asking while the buffer is clean', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm')
    await enterEditMode()
    expect(ask()).toBe(true)
    expect(confirmSpy).not.toHaveBeenCalled()
  })
})
