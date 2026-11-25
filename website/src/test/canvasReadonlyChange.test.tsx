/** The artifact write boundary rejects late edits without discarding an earlier valid draft. */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import CanvasArtifactBody from '../components/canvas/CanvasArtifactBody'
import CanvasEditor from '../components/canvas/CanvasEditor'
import { emptyCanvas, serializeCanvas, type Canvas } from '../components/canvas/canvasModel'
import { canvasDocument, flushCanvas } from '../components/canvas/canvasPending'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import type { Artifact } from '../types'
import { copyToClipboard } from '../utils/clipboard'

vi.mock('../components/canvas/CanvasEditor', () => ({ default: vi.fn(() => null) }))
vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(true) }))
vi.mock('../api/client', () => ({ api: { updateArtifact: vi.fn(), artifact: vi.fn() } }))

afterEach(() => { cleanup(); vi.clearAllMocks() })

const initial: Canvas = {
  ...emptyCanvas(),
  shapes: [{ id: 'a', kind: 'sticky', x: 0, y: 0, w: 200, h: 200, text: 'Original', color: 'yellow' }],
}
const artifact = (canvas: Canvas, extra: Partial<Artifact> = {}): Artifact => ({
  slug: 'board', name: 'Board', kind: 'canvas', version: 1,
  content: serializeCanvas(canvas), content_token: 't1', ...extra,
} as Artifact)

function mount(extra: Partial<Artifact> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const body = (art: Artifact, isCurrent = true) => (
    <QueryClientProvider client={qc}><CanvasArtifactBody artifact={art} isCurrent={isCurrent} /></QueryClientProvider>
  )
  const view = render(body(artifact(initial, extra)))
  const onChange = vi.mocked(CanvasEditor).mock.calls[0][0].onChange
  return { ...view, body, onChange }
}

describe('the canvas edit callback across read-only transitions', () => {
  it('refuses a retained edit callback after adopting a linked-file read', async () => {
    const { rerender, body, onChange } = mount()
    const linked = { ...initial, shapes: initial.shapes.map(s => ({ ...s, text: 'On disk' })) }
    rerender(body(artifact(linked, { content_token: undefined, source_path: '/boards/board.canvas' })))
    expect(vi.mocked(CanvasEditor).mock.lastCall?.[0].readOnly).toBe(true)
    act(() => { onChange({ ...linked, shapes: linked.shapes.map(s => ({ ...s, hex: '#123456' })) }) })
    await act(async () => { expect(await flushCanvas('board')).toBe(true) })
    expect(api.updateArtifact).not.toHaveBeenCalled()
    expect(canvasDocument('board')).toBe(serializeCanvas(linked))
  })

  it('still flushes a valid pending draft after switching to a past version', async () => {
    vi.mocked(api.updateArtifact).mockResolvedValueOnce({ content_token: 't2' } as Artifact)
    const { rerender, body, onChange } = mount()
    const draft = { ...initial, shapes: initial.shapes.map(s => ({ ...s, hex: '#123456' })) }
    act(() => { onChange(draft) })
    rerender(body(artifact(initial, { content_token: undefined }), false))
    expect(vi.mocked(CanvasEditor).mock.lastCall?.[0].readOnly).toBe(true)
    await act(async () => { expect(await flushCanvas('board')).toBe(true) })
    expect(api.updateArtifact).toHaveBeenCalledWith('board', {
      content: serializeCanvas(draft), snapshot: false, expected_token: 't1',
    })
  })
})

describe('a masked conflict', () => {
  it('copies the text of the shapes the owner edited, not the ones left as read', async () => {
    vi.mocked(api.updateArtifact).mockRejectedValueOnce(new ApiError(409, 'stale', '{}'))
    const two: Canvas = { ...initial, shapes: [...initial.shapes, { ...initial.shapes[0], id: 'b', text: 'Untouched' }] }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><CanvasArtifactBody artifact={artifact(two, { content_redacted: true })} isCurrent /></QueryClientProvider>)
    const onChange = vi.mocked(CanvasEditor).mock.calls[0][0].onChange
    act(() => { onChange({ ...two, shapes: two.shapes.map(s => (s.id === 'a' ? { ...s, text: 'Edited' } : s)) }) })
    await act(async () => { await flushCanvas('board') })
    await act(async () => { fireEvent.click(screen.getByText('Copy my text')) })
    expect(copyToClipboard).toHaveBeenCalledWith('Edited')
    expect(screen.getByText('Copied')).toBeTruthy()
  })

  it('says so when the browser refuses the copy, and lets the owner try again', async () => {
    vi.mocked(api.updateArtifact).mockRejectedValueOnce(new ApiError(409, 'stale', '{}'))
    const { onChange } = mount({ content_redacted: true })
    act(() => { onChange({ ...initial, shapes: initial.shapes.map(s => ({ ...s, text: 'Edited' })) }) })
    await act(async () => { await flushCanvas('board') })
    vi.mocked(copyToClipboard).mockResolvedValueOnce(false)
    await act(async () => { fireEvent.click(screen.getByText('Copy my text')) })
    expect(screen.getByText("Couldn't copy. Try again")).toBeTruthy()
    await act(async () => { fireEvent.click(screen.getByText("Couldn't copy. Try again")) })
    expect(screen.getByText('Copied')).toBeTruthy()
  })
})

describe('a Reload in flight', () => {
  it('ignores an edit made while it reads, so no save lands on the token it replaces', async () => {
    vi.mocked(api.updateArtifact).mockRejectedValueOnce(new ApiError(409, 'stale', '{}'))
    const { onChange } = mount()
    const first = { ...initial, shapes: initial.shapes.map(s => ({ ...s, text: 'First' })) }
    act(() => { onChange(first) })
    await act(async () => { await flushCanvas('board') })
    let answer!: (value: Artifact) => void
    vi.mocked(api.artifact).mockReturnValueOnce(new Promise<Artifact>(resolve => { answer = resolve }))
    act(() => { fireEvent.click(screen.getByText('Reload the canvas')) })
    act(() => { onChange({ ...initial, shapes: initial.shapes.map(s => ({ ...s, text: 'Second' })) }) })
    expect(canvasDocument('board')).toBe(serializeCanvas(first))
    await act(async () => { answer(artifact(initial, { content_token: 'tLive' })) })
    expect(api.updateArtifact).toHaveBeenCalledTimes(1)
  })
})

describe('the save reply of a masked canvas', () => {
  const epoch = () => vi.mocked(CanvasEditor).mock.lastCall?.[0].epoch
  const note = () => document.querySelector('[data-testid="canvas-masked-note"]')
  const maskedReply = (draft: Canvas) => serializeCanvas({ ...draft, shapes: draft.shapes.map(s => ({ ...s, text: 'key [REDACTED: credential]' })) })

  it('a reply that masks the typed text leaves the page its own copy, undo history and next save', async () => {
    const { onChange } = mount()
    const typed = { ...initial, shapes: initial.shapes.map(s => ({ ...s, text: 'key value' })) }
    vi.mocked(api.updateArtifact).mockResolvedValueOnce({ content: maskedReply(typed), content_token: 't2' } as Artifact)
    const before = epoch()
    act(() => { onChange(typed) })
    await act(async () => { expect(await flushCanvas('board')).toBe(true) })
    expect(epoch()).toBe(before)
    expect(canvasDocument('board')).toBe(serializeCanvas(typed))
    expect(note()).toBeNull()
    // A copy of the typed text goes out as the page holds it, with no marker for a merge.
    const copied = { ...typed, shapes: [...typed.shapes, { ...typed.shapes[0], id: 'b' }] }
    vi.mocked(api.updateArtifact).mockResolvedValueOnce({ content_token: 't3' } as Artifact)
    act(() => { onChange(copied) })
    await act(async () => { expect(await flushCanvas('board')).toBe(true) })
    expect(api.updateArtifact).toHaveBeenLastCalledWith('board', {
      content: serializeCanvas(copied), snapshot: false, expected_token: 't2',
    })
  })

  it('a page opened from a masked read says so and marks each save with that read\'s token', async () => {
    vi.mocked(api.updateArtifact).mockResolvedValue({ content_token: 't2' } as Artifact)
    const { onChange } = mount({ content_redacted: true })
    expect(note()).not.toBeNull()
    act(() => { onChange({ ...initial, shapes: initial.shapes.map(s => ({ ...s, text: 'Edited' })) }) })
    await act(async () => { expect(await flushCanvas('board')).toBe(true) })
    expect(vi.mocked(api.updateArtifact).mock.lastCall?.[1]).toMatchObject({ placeholders_token: 't1' })
  })
})
