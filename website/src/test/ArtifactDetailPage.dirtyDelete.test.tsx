/**
 * A remote deletion must not silently destroy an unsaved edit buffer.
 *
 * `artifact_update {deleted}` arrives over the WebSocket from any window, and the
 * detail page reacts by leaving the page. If the user has unsaved edits open, that
 * navigation discards them with no way back — so a dirty page keeps its content
 * and surfaces the deletion instead.
 *
 * This lives in its own file because reaching `dirty === true` requires an editor
 * that emits `onChange`, and the real one is Monaco, which renders no accessible
 * input under jsdom (no existing suite manages it — they all note "stay clean").
 * `ContentRenderer` is therefore mocked down to a textarea wired to `onChange`;
 * the subject under test is ArtifactDetailPage's guard, not the editor.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent, act } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { isArtifactEditing, __resetArtifactEditing } from '../utils/artifactEditGuard'
import type { Artifact } from '../types'

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))
// Replace ONLY the renderer; the module also exports MD_EXTS / extOf / wrapCode /
// langFor / CodeEditor, which other components import.
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

function renderPage() {
  return renderWithProviders(
    <Routes>
      <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
      <Route path="/artifacts" element={<div>library page target</div>} />
    </Routes>,
    { route: '/artifacts/cr-queue' },
  )
}

function fireDeleted(slug = 'cr-queue') {
  act(() => {
    window.dispatchEvent(new CustomEvent('kirocrew:artifact-deleted', { detail: { slug } }))
  })
}

describe('ArtifactDetailPage remote deletion vs unsaved edits', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    __resetArtifactEditing()
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'cr-queue', versions: [1] })
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'cr-queue', events: [] })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
  })

  async function enterEditMode() {
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Toggle agent chat')).toBeInTheDocument())
    fireEvent.click(screen.getByTitle(/edit content/i))
    return screen.findByLabelText('editor')
  }

  it('guards for the whole edit lifetime, not just once dirty', async () => {
    // The exposure window opens when the editor OPENS. A dirty-only guard would let
    // an update land while the editor sat open-and-clean, moving the baseline, and
    // the next keystroke would make the stale buffer dirty and Save would overwrite.
    const editor = await enterEditMode()
    expect(isArtifactEditing('cr-queue')).toBe(true)      // clean, but already open
    fireEvent.change(editor, { target: { value: '# unsaved work' } })
    expect(isArtifactEditing('cr-queue')).toBe(true)
    // Reverting the text does NOT reopen the window — the editor is still open.
    fireEvent.change(screen.getByLabelText('editor'), { target: { value: '# v1' } })
    expect(isArtifactEditing('cr-queue')).toBe(true)
  })

  it('clears the guard and refetches when editing ends', async () => {
    // Whatever was withheld while the editor was open has to be picked up, or the
    // page keeps showing pre-update content indefinitely.
    await enterEditMode()
    expect(isArtifactEditing('cr-queue')).toBe(true)
    const before = vi.mocked(api).artifact.mock.calls.length
    fireEvent.click(screen.getByTitle(/cancel/i))
    await waitFor(() => expect(isArtifactEditing('cr-queue')).toBe(false))
    await waitFor(() =>
      expect(vi.mocked(api).artifact.mock.calls.length).toBeGreaterThan(before))
  })

  it('keeps the page and surfaces the deletion when the buffer is dirty', async () => {
    const editor = await enterEditMode()
    fireEvent.change(editor, { target: { value: '# unsaved work' } })
    fireDeleted()
    await waitFor(() =>
      expect(screen.getByText(/this artifact was deleted/i)).toBeInTheDocument())
    expect(screen.queryByText('library page target')).toBeNull()
    // The unsaved content is still on screen, so it can be copied out.
    expect((screen.getByLabelText('editor') as HTMLTextAreaElement).value).toBe('# unsaved work')
  })

  it('navigates away when editing but NOT dirty', async () => {
    // Being in edit mode is not itself a reason to strand the user.
    await enterEditMode()
    fireDeleted()
    await waitFor(() => expect(screen.getByText('library page target')).toBeInTheDocument())
  })

  it('ignores a deletion for a different artifact while dirty', async () => {
    const editor = await enterEditMode()
    fireEvent.change(editor, { target: { value: '# unsaved work' } })
    fireDeleted('some-other-slug')
    expect(screen.queryByText(/this artifact was deleted/i)).toBeNull()
    expect(screen.queryByText('library page target')).toBeNull()
  })
})
