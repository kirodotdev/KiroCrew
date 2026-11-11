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
import { chooseMore, commentsShown, findMoreItem } from './artifactMoreMenu'

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
  act(() => { (screen.getByTestId('canvas-shape-editor') as HTMLTextAreaElement).blur() })
}

beforeEach(() => {
  vi.mocked(api).artifact = vi.fn().mockResolvedValue(artifact)
  vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'board', versions: [1] })
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

  it('asks once for a comment drafted during session creation, then navigates after recheck', async () => {
    const create = deferred<{ key: string }>()
    vi.mocked(api.createChatSlot).mockReturnValueOnce(create.promise)
    await renderPage()
    if (!(await commentsShown())) await chooseMore(/Show comments/)
    await startNewSession()
    expect(api.createChatSlot).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: /Add comment/ }))
    fireEvent.change(screen.getByPlaceholderText('Add a comment on the whole artifact…'), { target: { value: 'unsent comment' } })
    await act(async () => { create.resolve({ key: 'chat-new' }); await vi.advanceTimersByTimeAsync(0) })
    expect(screen.queryByTestId('chat-target')).toBeNull()
    const dialog = screen.getByRole('dialog')
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: /discard comment/i })); await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByTestId('chat-target')).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
