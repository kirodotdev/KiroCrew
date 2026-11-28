import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { NavigationLeaveGuardProvider } from '../components/NavigationLeaveGuard'
import { emptyCanvas, serializeCanvas } from '../components/canvas/canvasModel'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import type { Artifact } from '../types'
import { renderWithProviders } from './helpers'
import { chooseMore, findMoreItem } from './artifactMoreMenu'

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({ default: () => <div />, PREFILL_STORAGE_KEY: 'kirocrew_prefill' }))

const artifact: Artifact = {
  slug: 'board', name: 'Board', kind: 'canvas', source: 'manual', description: '', tags: [],
  version: 1, content_token: 't1', content: serializeCanvas(emptyCanvas()),
  created_at: '2026-11-13T12:00:00Z', updated_at: '2026-11-13T12:00:00Z',
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

async function renderPage() {
  const view = renderWithProviders(
    <NavigationLeaveGuardProvider>
      <Routes>
        <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
        <Route path="/chat" element={<div data-testid="chat-target" />} />
      </Routes>
    </NavigationLeaveGuardProvider>,
    { route: '/artifacts/board' },
  )
  // The canvas editor is lazy-loaded; wait for that boundary, not just the header.
  await screen.findByTestId('canvas-editor', {}, { timeout: 5000 })
  await waitFor(() => {
    expect(api.artifactVersions).toHaveBeenCalled()
    expect(api.artifactEvents).toHaveBeenCalled()
    expect(api.artifactComments).toHaveBeenCalled()
  })
  return view
}

async function startNewSession() {
  fireEvent.click(await findMoreItem('Send to a session'))
  const item = await screen.findByRole('menuitem', { name: 'New session' })
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-13T12:00:00Z'))
  await act(async () => { fireEvent.click(item); await vi.advanceTimersByTimeAsync(0) })
}

function addSticky() {
  fireEvent.click(screen.getByTestId('tool-sticky'))
  fireEvent.pointerDown(screen.getByTestId('canvas-editor'), { clientX: 400, clientY: 400, button: 0, pointerId: 1 })
  fireEvent.pointerUp(window, { clientX: 400, clientY: 400, button: 0, pointerId: 1 })
  const editor = screen.getByTestId('canvas-shape-editor') as HTMLTextAreaElement
  fireEvent.change(editor, { target: { value: 'Note' } })
  act(() => { editor.blur() })
}

beforeEach(() => {
  vi.mocked(api).artifact = vi.fn().mockResolvedValue(artifact)
  vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'board', versions: [1] })
  vi.mocked(api).artifactVersion = vi.fn().mockResolvedValue(artifact)
  vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'board', events: [] })
  vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
  vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
  vi.mocked(api).updateArtifact = vi.fn().mockResolvedValue({ ...artifact, content_token: 't2' })
  vi.mocked(api).createChatSlot = vi.fn().mockResolvedValue({ key: 'chat-new' })
})

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })

describe('canvas session handoff', () => {
  it('flushes edits made during session creation and holds navigation while that save is waiting', async () => {
    const create = deferred<{ key: string }>()
    const save = deferred<Artifact>()
    vi.mocked(api.createChatSlot).mockReturnValueOnce(create.promise)
    vi.mocked(api.updateArtifact).mockReturnValueOnce(save.promise)
    const { store } = await renderPage()
    const active = store.getState().chat.activeSlot
    await startNewSession()
    expect(api.createChatSlot).toHaveBeenCalledTimes(1)
    addSticky()
    expect(api.updateArtifact).not.toHaveBeenCalled()
    await act(async () => { create.resolve({ key: 'chat-new' }); await vi.advanceTimersByTimeAsync(0) })
    expect(screen.queryByTestId('chat-target')).toBeNull()
    expect(store.getState().chat.activeSlot).toBe(active)
    expect(api.updateArtifact).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('canvas-save-status')).toHaveTextContent('Saving')
    const saved = { ...artifact, content: vi.mocked(api.updateArtifact).mock.calls[0][1].content!, content_token: 't2' }
    vi.mocked(api.artifact).mockResolvedValue(saved)
    await act(async () => { save.resolve(saved); await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByTestId('canvas-save-status')).toHaveTextContent('Saved')
    expect(screen.getAllByTestId('canvas-shape')).toHaveLength(1)
    // A clean exit now passes the same dispatcher without another save or prompt.
    vi.useRealTimers()
    await startNewSession()
    expect(screen.getByTestId('chat-target')).toBeInTheDocument()
    expect(api.updateArtifact).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it.each(['failed', 'conflict'] as const)('asks before leaving edits whose save %s during session creation', async state => {
    const create = deferred<{ key: string }>()
    vi.mocked(api.createChatSlot).mockReturnValueOnce(create.promise)
    vi.mocked(api.updateArtifact).mockRejectedValueOnce(state === 'conflict' ? new ApiError(409, 'stale', '{}') : new Error('offline'))
    const { store } = await renderPage()
    const active = store.getState().chat.activeSlot
    await startNewSession()
    expect(api.createChatSlot).toHaveBeenCalledTimes(1)
    addSticky()
    await act(async () => { await vi.advanceTimersByTimeAsync(800) })
    expect(api.updateArtifact).toHaveBeenCalledTimes(1)
    await act(async () => { create.resolve({ key: 'chat-new' }); await vi.advanceTimersByTimeAsync(0) })
    expect(screen.queryByTestId('chat-target')).toBeNull()
    expect(store.getState().chat.activeSlot).toBe(active)
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText('Discard unsaved changes?')).toBeInTheDocument()
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i })); await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getAllByTestId('canvas-shape')).toHaveLength(1)
    expect(screen.queryByTestId('chat-target')).toBeNull()
  })

  // The canvas page opens comments only from More > Show comments, and every comment there is
  // on the whole artifact.
  it('asks once for a comment drafted during session creation, then navigates after recheck', async () => {
    const create = deferred<{ key: string }>()
    vi.mocked(api.createChatSlot).mockReturnValueOnce(create.promise)
    await renderPage()
    await chooseMore('Show comments')
    const comments = await screen.findByTestId('canvas-page-comments')
    await startNewSession()
    expect(api.createChatSlot).toHaveBeenCalledTimes(1)
    fireEvent.click(within(comments).getByRole('button', { name: 'Add comment' }))
    fireEvent.change(within(comments).getByPlaceholderText('Add a comment on the whole artifact…'), { target: { value: 'unsent comment' } })
    await act(async () => { create.resolve({ key: 'chat-new' }); await vi.advanceTimersByTimeAsync(0) })
    expect(screen.queryByTestId('chat-target')).toBeNull()
    const dialogs = screen.getAllByRole('dialog')
    expect(dialogs).toHaveLength(1)
    await act(async () => { fireEvent.click(within(dialogs[0]).getByRole('button', { name: /discard comment/i })); await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByTestId('chat-target')).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})

// The canvas page has no Compare control; the version picker is the view switch the
// waiting save must survive.
describe('canvas version navigation', () => {
  async function pickVersion(label: string) {
    fireEvent.keyDown(screen.getByRole('button', { name: 'Version' }), { key: 'Enter' })
    await act(async () => {
      fireEvent.click(screen.getByRole('menuitemradio', { name: label, exact: true }))
      await vi.advanceTimersByTimeAsync(0)
    })
  }

  it.each(['failed', 'conflict'] as const)('keeps the draft mounted when a version switch is cancelled after a %s save', async state => {
    vi.mocked(api.updateArtifact).mockRejectedValue(state === 'conflict' ? new ApiError(409, 'stale', '{}') : new Error('offline'))
    await renderPage()
    vi.useFakeTimers()
    addSticky()
    await act(async () => { await vi.advanceTimersByTimeAsync(800) })
    expect(api.updateArtifact).toHaveBeenCalledTimes(1)
    const editor = screen.getByTestId('canvas-editor')

    await pickVersion('v1')
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText('Discard unsaved changes?')).toBeInTheDocument()
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i }))
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByTestId('canvas-editor')).toBe(editor)
    expect(screen.getAllByTestId('canvas-shape')).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Version' })).toHaveTextContent('Live')

    await pickVersion('v1')
    await act(async () => {
      fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Discard changes' }))
      await vi.advanceTimersByTimeAsync(0)
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    expect(api.artifactVersion).toHaveBeenCalledWith('board', 1)
    expect(screen.getByRole('button', { name: 'Version' })).toHaveTextContent('v1')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('waits for a successful flush before switching version without asking', async () => {
    const save = deferred<Artifact>()
    vi.mocked(api.updateArtifact).mockReturnValueOnce(save.promise)
    await renderPage()
    vi.useFakeTimers()
    addSticky()
    await pickVersion('v1')
    expect(api.updateArtifact).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('canvas-save-status')).toHaveTextContent('Saving')
    expect(screen.getByRole('button', { name: 'Version' })).toHaveTextContent('Live')
    expect(screen.queryByRole('dialog')).toBeNull()

    const saved = { ...artifact, content: vi.mocked(api.updateArtifact).mock.calls[0][1].content!, content_token: 't2' }
    vi.mocked(api.artifact).mockResolvedValue(saved)
    await act(async () => { save.resolve(saved); await vi.advanceTimersByTimeAsync(0) })
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    expect(api.artifactVersion).toHaveBeenCalledWith('board', 1)
    expect(screen.getByRole('button', { name: 'Version' })).toHaveTextContent('v1')
    expect(api.updateArtifact).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
