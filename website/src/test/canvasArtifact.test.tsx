/**
 * A canvas artifact on its detail page: it saves itself after a pause, never
 * over content changed elsewhere, never over a document it cannot read, and a
 * past version only pans and zooms. Plus the library thumbnail.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { ApiError } from '../api/apiError'
import type { Artifact } from '../types'
import { emptyCanvas, parseCanvas, serializeCanvas, type Canvas } from '../components/canvas/canvasModel'

const updateArtifact = vi.fn()
const artifact = vi.fn()
vi.mock('../api/client', () => ({ api: { updateArtifact: (...a: unknown[]) => updateArtifact(...a), artifact: (...a: unknown[]) => artifact(...a) } }))

const { default: CanvasArtifactBody } = await import('../components/canvas/CanvasArtifactBody')
const { default: CanvasThumb } = await import('../components/canvas/CanvasThumb')

const withShapes = (n: number): Canvas => ({
  ...emptyCanvas(),
  shapes: Array.from({ length: n }, (_, i) => ({ id: `s${i}`, kind: 'sticky' as const, x: i * 240, y: 0, w: 200, h: 200, text: '', color: 'yellow' as const })),
})

const art = (content: string, extra: Partial<Artifact> = {}): Artifact => ({
  slug: 'board', name: 'Board', kind: 'canvas', content, content_token: 't1', version: 1,
  ...extra,
} as Artifact)

function wrap(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

/** Drop a sticky with the dock: the one edit every test makes. */
function addSticky() {
  fireEvent.click(screen.getByTestId('tool-sticky'))
  const canvas = screen.getByTestId('canvas-editor')
  act(() => { fireEvent.pointerDown(canvas, { clientX: 400, clientY: 400, button: 0, pointerId: 1 }) })
  act(() => { fireEvent.pointerUp(window, { clientX: 400, clientY: 400, button: 0, pointerId: 1 }) })
  // The new sticky opens for typing; leaving it empty ends the edit.
  act(() => { (screen.getByTestId('canvas-shape-editor') as HTMLTextAreaElement).blur() })
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  cleanup()
  updateArtifact.mockReset()
  artifact.mockReset()
})

describe('a canvas artifact', () => {
  it('saves a pause after the last change, as a live save on the token it read, and chains the next token', async () => {
    updateArtifact.mockResolvedValueOnce({ content_token: 't2' }).mockResolvedValueOnce({ content_token: 't3' })
    render(wrap(<CanvasArtifactBody artifact={art(serializeCanvas(emptyCanvas()))} isCurrent />))
    expect(screen.getByTestId('canvas-save-status').textContent).toBe('Saved')

    addSticky()
    expect(screen.getByTestId('canvas-save-status').textContent).toBe('Unsaved changes')
    expect(updateArtifact).not.toHaveBeenCalled()
    await act(async () => { await vi.advanceTimersByTimeAsync(800) })
    expect(updateArtifact).toHaveBeenCalledTimes(1)
    const [slug, body] = updateArtifact.mock.calls[0] as [string, { content: string; snapshot: boolean; expected_token: string }]
    expect(slug).toBe('board')
    expect(body.snapshot).toBe(false)
    expect(body.expected_token).toBe('t1')
    const saved = parseCanvas(body.content)
    expect(saved.ok && saved.canvas.shapes.map(s => s.kind)).toEqual(['sticky'])
    expect(screen.getByTestId('canvas-save-status').textContent).toBe('Saved')

    addSticky()
    await act(async () => { await vi.advanceTimersByTimeAsync(800) })
    expect((updateArtifact.mock.calls[1][1] as { expected_token: string }).expected_token).toBe('t2')
  })

  it('a canvas changed elsewhere stops saving and asks; keeping mine saves on the live token', async () => {
    updateArtifact.mockRejectedValueOnce(new ApiError(409, 'stale', JSON.stringify({ current_token: 'tX' })))
    render(wrap(<CanvasArtifactBody artifact={art(serializeCanvas(emptyCanvas()))} isCurrent />))
    addSticky()
    await act(async () => { await vi.advanceTimersByTimeAsync(800) })
    expect(screen.getByText('This canvas changed somewhere else')).toBeTruthy()

    // Further edits wait for the person's choice rather than retrying into the same refusal.
    addSticky()
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(updateArtifact).toHaveBeenCalledTimes(1)

    artifact.mockResolvedValueOnce({ ...art(serializeCanvas(withShapes(3))), content_token: 'tLive' })
    updateArtifact.mockResolvedValueOnce({ content_token: 'tMine' })
    await act(async () => { fireEvent.click(screen.getByText('Keep my version')); await vi.advanceTimersByTimeAsync(0) })
    expect(updateArtifact).toHaveBeenCalledTimes(2)
    const body = updateArtifact.mock.calls[1][1] as { content: string; expected_token: string }
    expect(body.expected_token).toBe('tLive')
    const mine = parseCanvas(body.content)
    expect(mine.ok && mine.canvas.shapes).toHaveLength(2)
    expect(screen.queryByText('This canvas changed somewhere else')).toBeNull()
  })

  it('reloading after a conflict drops my edits and shows what is stored now', async () => {
    updateArtifact.mockRejectedValueOnce(new ApiError(409, 'stale', '{}'))
    render(wrap(<CanvasArtifactBody artifact={art(serializeCanvas(emptyCanvas()))} isCurrent />))
    addSticky()
    await act(async () => { await vi.advanceTimersByTimeAsync(800) })
    artifact.mockResolvedValueOnce({ ...art(serializeCanvas(withShapes(3))), content_token: 'tLive' })
    await act(async () => { fireEvent.click(screen.getByText('Reload the canvas')); await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getAllByTestId('canvas-shape')).toHaveLength(3)
    expect(updateArtifact).toHaveBeenCalledTimes(1)
  })

  it('a document that is not a canvas is never opened for editing, so nothing is saved over it', async () => {
    render(wrap(<CanvasArtifactBody artifact={art('# notes, not a canvas')} isCurrent />))
    expect(screen.getByText("This canvas can't be opened")).toBeTruthy()
    expect(screen.queryByTestId('canvas-editor')).toBeNull()
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(updateArtifact).not.toHaveBeenCalled()
  })

  it('a past version pans and zooms but has no tools and never saves', async () => {
    render(wrap(<CanvasArtifactBody artifact={art(serializeCanvas(withShapes(2)))} isCurrent={false} />))
    expect(screen.getAllByTestId('canvas-shape')).toHaveLength(2)
    expect(screen.queryByTestId('canvas-dock')).toBeNull()
    expect(screen.getByTestId('canvas-readonly-note')).toBeTruthy()
    const canvas = screen.getByTestId('canvas-editor')
    act(() => { fireEvent.doubleClick(canvas, { clientX: 400, clientY: 600 }) })
    act(() => { fireEvent.keyDown(canvas, { key: 'Backspace' }) })
    expect(screen.getAllByTestId('canvas-shape')).toHaveLength(2)
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(updateArtifact).not.toHaveBeenCalled()
  })
})

describe('the canvas thumbnail', () => {
  it('draws one mark per shape, and says so when the canvas is empty', () => {
    const { container, unmount } = render(<CanvasThumb content={serializeCanvas(withShapes(3))} />)
    expect(container.querySelectorAll('svg rect')).toHaveLength(3)
    unmount()
    render(<CanvasThumb content={serializeCanvas(emptyCanvas())} />)
    expect(screen.getByText('Empty canvas')).toBeTruthy()
  })
})
