/** Deferred server reads follow the canvas's clean transitions, never its own stale echoes. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import CanvasArtifactBody from '../components/canvas/CanvasArtifactBody'
import CanvasEditor from '../components/canvas/CanvasEditor'
import { emptyCanvas, serializeCanvas, type Canvas } from '../components/canvas/canvasModel'
import { canvasDocument, discardCanvas, flushCanvas } from '../components/canvas/canvasPending'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import type { Artifact } from '../types'

vi.mock('../components/canvas/CanvasEditor', () => ({ default: vi.fn(() => null) }))
vi.mock('../api/client', () => ({ api: { updateArtifact: vi.fn(), artifact: vi.fn() } }))

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { cleanup(); vi.useRealTimers(); vi.resetAllMocks() })

const document = (text: string): Canvas => ({
  ...emptyCanvas(),
  shapes: [{ id: 'a', kind: 'sticky', x: 0, y: 0, w: 200, h: 200, text, color: 'yellow' }],
})
const A = document('A')
const B = document('B')
const C = document('C')
const artifact = (canvas: Canvas, token: string | undefined, redacted = false): Artifact => ({
  slug: 'board', name: 'Board', kind: 'canvas', version: 1,
  content: serializeCanvas(canvas), content_token: token, content_redacted: redacted,
  source_path: token ? undefined : '/boards/board.canvas',
} as Artifact)
const editor = () => vi.mocked(CanvasEditor).mock.lastCall![0]

function mount(initial = artifact(A, 'tA')) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const body = (read: Artifact, isCurrent = true) => (
    <QueryClientProvider client={qc}><CanvasArtifactBody artifact={read} isCurrent={isCurrent} /></QueryClientProvider>
  )
  const view = render(body(initial))
  return {
    read: (canvas: Canvas, token: string | undefined, redacted = false, isCurrent = true) => view.rerender(body(artifact(canvas, token, redacted), isCurrent)),
    change: (canvas: Canvas) => act(() => { editor().onChange(canvas) }),
  }
}

async function tick() {
  await act(async () => { await vi.advanceTimersByTimeAsync(700) })
}

async function nextSave(change: (canvas: Canvas) => void, token: string) {
  vi.mocked(api.updateArtifact).mockResolvedValueOnce({ content_token: 'tNext' } as Artifact)
  change(document('Next'))
  await tick()
  expect(api.updateArtifact).toHaveBeenLastCalledWith('board', {
    content: serializeCanvas(document('Next')), snapshot: false, expected_token: token,
  })
  expect(screen.getByTestId('canvas-save-status')).toHaveTextContent('Saved')
  expect(screen.queryByText('This canvas changed somewhere else')).toBeNull()
}

function expectShown(canvas: Canvas) {
  expect(canvasDocument('board')).toBe(serializeCanvas(canvas))
  expect(editor().readOnly).toBe(false)
  expect(screen.getByTestId('canvas-save-status')).toHaveTextContent('Saved')
}

describe('canvas read reconciliation', () => {
  it('adopts refreshed untokened live bytes while keeping a linked canvas read-only', async () => {
    const { read, change } = mount(artifact(A, undefined))
    const epoch = editor().epoch
    expect(editor().readOnly).toBe(true)
    read(B, undefined)
    expect(serializeCanvas(editor().canvas)).toBe(serializeCanvas(B))
    expect(canvasDocument('board')).toBe(serializeCanvas(B))
    expect(editor().epoch).toBeGreaterThan(epoch!)
    expect(editor().readOnly).toBe(true)
    change(C)
    await tick()
    expect(serializeCanvas(editor().canvas)).toBe(serializeCanvas(B))
    expect(api.updateArtifact).not.toHaveBeenCalled()
  })

  it('restores untokened live bytes after viewing a historical snapshot', () => {
    const { read } = mount(artifact(A, undefined))
    read(B, undefined, false, false)
    expect(serializeCanvas(editor().canvas)).toBe(serializeCanvas(B))
    expect(editor().readOnly).toBe(true)
    expect(canvasDocument('board')).toBeNull()
    read(A, undefined)
    expect(serializeCanvas(editor().canvas)).toBe(serializeCanvas(A))
    expect(canvasDocument('board')).toBe(serializeCanvas(A))
    expect(editor().readOnly).toBe(true)
    expect(api.updateArtifact).not.toHaveBeenCalled()
  })

  it.each([
    ['same', A, 'tA'],
    ['foreign', C, 'tC'],
  ] as const)('returns from history to a %s live token and saves on that token', async (_, live, token) => {
    const { read, change } = mount()
    read(B, undefined, false, false)
    expect(serializeCanvas(editor().canvas)).toBe(serializeCanvas(B))
    expect(editor().readOnly).toBe(true)
    expect(canvasDocument('board')).toBeNull()
    change(C)
    await tick()
    expect(serializeCanvas(editor().canvas)).toBe(serializeCanvas(B))
    expect(api.updateArtifact).not.toHaveBeenCalled()
    const epoch = editor().epoch
    read(live, token)
    expect(serializeCanvas(editor().canvas)).toBe(serializeCanvas(live))
    expectShown(live)
    expect(editor().epoch).toBeGreaterThan(epoch!)
    await nextSave(change, token)
  })

  it('restores saved live bytes before ignoring an own-token echo after multiple snapshots', async () => {
    vi.mocked(api.updateArtifact).mockResolvedValueOnce({ content_token: 'tS' } as Artifact)
    const { read, change } = mount()
    change(B)
    await tick()
    read(A, undefined, false, false)
    read(C, undefined, false, false)
    act(() => { discardCanvas('board') })
    expect(serializeCanvas(editor().canvas)).toBe(serializeCanvas(C))
    read(A, 'tA')
    expect(serializeCanvas(editor().canvas)).toBe(serializeCanvas(B))
    expectShown(B)
    await nextSave(change, 'tS')
  })

  it('adopts a deferred foreign read when undo returns the draft to its base', async () => {
    const { read, change } = mount()
    const epoch = editor().epoch
    change(B)
    read(C, 'tC')
    expect(canvasDocument('board')).toBe(serializeCanvas(B))
    change(A)
    await tick()
    expect(api.updateArtifact).not.toHaveBeenCalled()
    expectShown(C)
    expect(editor().epoch).toBeGreaterThan(epoch!)
    await nextSave(change, 'tC')
  })

  it('adopts a read deferred during an in-flight save after that save succeeds', async () => {
    let resolve!: (value: Artifact) => void
    const pending = new Promise<Artifact>(r => { resolve = r })
    vi.mocked(api.updateArtifact).mockReturnValueOnce(pending)
    const { read, change } = mount()
    change(B)
    await tick()
    expect(api.updateArtifact).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('canvas-save-status')).toHaveTextContent('Saving')
    read(C, 'tC')
    expect(canvasDocument('board')).toBe(serializeCanvas(B))
    await act(async () => { resolve({ content_token: 'tS' } as Artifact); await pending })
    expectShown(C)
    await nextSave(change, 'tC')
  })

  it('adopts a deferred foreign read after discard', async () => {
    const { read, change } = mount()
    change(B)
    read(C, 'tC')
    act(() => { discardCanvas('board') })
    expectShown(C)
    expect(api.updateArtifact).not.toHaveBeenCalled()
    await nextSave(change, 'tC')
  })

  it('discard with only an own-token read restores the saved base without rolling back its token', async () => {
    vi.mocked(api.updateArtifact).mockResolvedValueOnce({ content_token: 'tS' } as Artifact)
    const { change } = mount()
    change(B)
    await tick()
    change(C)
    act(() => { discardCanvas('board') })
    expectShown(B)
    await nextSave(change, 'tS')
  })

  it.each([false, true])('ignores stale and saved own echoes (masked: %s), preserving undo and the save token', async masked => {
    vi.mocked(api.updateArtifact).mockResolvedValueOnce({ content_token: 'tS' } as Artifact)
    const { read, change } = mount()
    change(B)
    await tick()
    const epoch = editor().epoch
    read(B, 'tS')
    expectShown(B)
    expect(editor().epoch).toBe(epoch)
    // A newly delivered read can still carry the pre-save token and bytes.
    read(masked ? document('[REDACTED]') : A, 'tA', masked)
    expectShown(B)
    expect(editor().epoch).toBe(epoch)
    read(masked ? document('[REDACTED]') : B, 'tS', masked)
    expectShown(B)
    expect(editor().epoch).toBe(epoch)
    await nextSave(change, 'tS')
  })

  it.each([false, true])('adopts redaction status independently of identical bytes after Reload (masked: %s)', async masked => {
    vi.mocked(api.updateArtifact).mockRejectedValueOnce(new ApiError(409, 'stale', '{}'))
    const { read, change } = mount()
    change(B)
    await tick()
    const hidden = document('[REDACTED]')
    vi.mocked(api.artifact).mockResolvedValueOnce(artifact(hidden, 'tReload', true))
    await act(async () => { fireEvent.click(screen.getByText('Reload the canvas')) })
    expect(editor().readOnly).toBe(true)
    expect(screen.getByTestId('canvas-readonly-note')).toHaveTextContent('Hidden text')
    const epoch = editor().epoch
    read(hidden, 'tForeign', masked)
    expect(canvasDocument('board')).toBe(serializeCanvas(hidden))
    expect(editor().epoch).toBe(epoch)
    expect(editor().readOnly).toBe(masked)
    if (masked) {
      expect(screen.getByTestId('canvas-readonly-note')).toHaveTextContent('Hidden text')
      change(C)
      await tick()
      expect(canvasDocument('board')).toBe(serializeCanvas(hidden))
      expect(api.updateArtifact).toHaveBeenCalledTimes(1)
    } else {
      expect(screen.queryByTestId('canvas-readonly-note')).toBeNull()
      await nextSave(change, 'tForeign')
    }
  })

  it.each([
    ['base-token echo', 'tReload', false],
    ['untokened identical read', undefined, false],
    ['untokened changed read', undefined, true],
  ] as const)('does not clear Reload redaction from a %s', async (_, token, changed) => {
    vi.mocked(api.updateArtifact).mockRejectedValueOnce(new ApiError(409, 'stale', '{}'))
    const { read, change } = mount()
    change(B)
    await tick()
    const hidden = document('[REDACTED]')
    vi.mocked(api.artifact).mockResolvedValueOnce(artifact(hidden, 'tReload', true))
    await act(async () => { fireEvent.click(screen.getByText('Reload the canvas')) })
    read(changed ? C : hidden, token)
    // An untokened read may refresh the view, but cannot clear Reload's mask state.
    read(changed ? C : hidden, 'tReload')
    expect(editor().readOnly).toBe(true)
    expect(screen.getByTestId('canvas-readonly-note')).toHaveTextContent('Hidden text')
    change(A)
    await tick()
    expect(api.updateArtifact).toHaveBeenCalledTimes(1)
  })

  it('keeps a deferred read out during conflict and reloads the fresh response ahead of the page read', async () => {
    vi.mocked(api.updateArtifact).mockRejectedValueOnce(new ApiError(409, 'stale', '{}'))
    const { read, change } = mount()
    change(B)
    await tick()
    read(C, 'tC')
    expect(canvasDocument('board')).toBe(serializeCanvas(B))
    expect(screen.getByText('This canvas changed somewhere else')).toBeTruthy()
    const fresh = document('Fresh reload')
    vi.mocked(api.artifact).mockResolvedValueOnce(artifact(fresh, 'tFresh'))
    await act(async () => { fireEvent.click(screen.getByText('Reload the canvas')) })
    expectShown(fresh)
    await act(async () => { expect(await flushCanvas('board')).toBe(true) })
    await nextSave(change, 'tFresh')
  })
})
