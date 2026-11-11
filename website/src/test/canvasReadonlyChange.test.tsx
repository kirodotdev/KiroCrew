/** The artifact write boundary rejects late edits without discarding an earlier valid draft. */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import CanvasArtifactBody from '../components/canvas/CanvasArtifactBody'
import CanvasEditor from '../components/canvas/CanvasEditor'
import { emptyCanvas, serializeCanvas, type Canvas } from '../components/canvas/canvasModel'
import { canvasDocument, flushCanvas } from '../components/canvas/canvasPending'
import { api } from '../api/client'
import type { Artifact } from '../types'

vi.mock('../components/canvas/CanvasEditor', () => ({ default: vi.fn(() => null) }))
vi.mock('../api/client', () => ({ api: { updateArtifact: vi.fn() } }))

afterEach(() => { cleanup(); vi.clearAllMocks() })

const initial: Canvas = {
  ...emptyCanvas(),
  shapes: [{ id: 'a', kind: 'sticky', x: 0, y: 0, w: 200, h: 200, text: 'Original', color: 'yellow' }],
}
const artifact = (canvas: Canvas, extra: Partial<Artifact> = {}): Artifact => ({
  slug: 'board', name: 'Board', kind: 'canvas', version: 1,
  content: serializeCanvas(canvas), content_token: 't1', ...extra,
} as Artifact)

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const body = (art: Artifact, isCurrent = true) => (
    <QueryClientProvider client={qc}><CanvasArtifactBody artifact={art} isCurrent={isCurrent} /></QueryClientProvider>
  )
  const view = render(body(artifact(initial)))
  const onChange = vi.mocked(CanvasEditor).mock.calls[0][0].onChange
  return { ...view, body, onChange }
}

describe('the canvas edit callback across read-only transitions', () => {
  it('refuses a retained edit callback after adopting masked content', async () => {
    const { rerender, body, onChange } = mount()
    const masked = { ...initial, shapes: initial.shapes.map(s => ({ ...s, text: '[REDACTED]' })) }
    rerender(body(artifact(masked, { content_redacted: true, content_token: 'tMasked' })))
    expect(vi.mocked(CanvasEditor).mock.lastCall?.[0].readOnly).toBe(true)
    act(() => { onChange({ ...masked, shapes: masked.shapes.map(s => ({ ...s, hex: '#123456' })) }) })
    await act(async () => { expect(await flushCanvas('board')).toBe(true) })
    expect(api.updateArtifact).not.toHaveBeenCalled()
    expect(canvasDocument('board')).toBe(serializeCanvas(masked))
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
