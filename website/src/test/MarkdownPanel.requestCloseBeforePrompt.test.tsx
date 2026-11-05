/**
 * `MarkdownPanelHandle.requestClose`'s `beforePrompt`: a host closing this tab
 * from outside (the side panel's tab strip) is told just before the close guard
 * shows a question, and not at all when the close needs none.
 */
import { describe, it, expect, vi } from 'vitest'
import { createRef, forwardRef, useImperativeHandle } from 'react'
import { act, render, screen } from '@testing-library/react'
import type { MarkdownPanelHandle } from '../components/MarkdownPanel'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PierreEditorHandle } from '../pierre'

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreEditor: forwardRef<PierreEditorHandle, { file: { contents: string } }>(
    function PierreEditorStub({ file }, ref) {
      useImperativeHandle(ref, () => ({ jumpToLine: () => {}, focus: () => {} }), [])
      return <div data-testid="pierre-editor" data-value={file.contents} />
    },
  ),
  PierreCode: ({ file }: { file: { contents: string } }) => <div data-testid="pierre-code" data-value={file.contents} />,
  PierreFilePair: () => <div data-testid="pierre-diff" />,
}))

vi.mock('../api/client', () => ({
  api: { artifacts: vi.fn(), artifact: vi.fn(), revealPath: vi.fn(), fileDiff: vi.fn() },
}))

class StubHighlight { constructor(..._ranges: Range[]) {} }
vi.stubGlobal('Highlight', StubHighlight)
vi.stubGlobal('CSS', { highlights: { set: () => {}, delete: () => true } })

const { default: MarkdownPanel } = await import('../components/MarkdownPanel')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter>
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  </MemoryRouter>
)

function mount(content: string, savedBaseline: string, onClose: () => void) {
  const ref = createRef<MarkdownPanelHandle>()
  render(<MarkdownPanel embedded ref={ref} filePath="/tmp/notes.md" content={content}
    savedBaseline={savedBaseline} onContentChange={vi.fn()} onSave={vi.fn(async () => {})}
    onClose={onClose} initialDiffMode={false} />, { wrapper })
  return ref
}

describe('MarkdownPanel requestClose beforePrompt', () => {
  it('runs just before the close guard asks about unsaved edits', async () => {
    const beforePrompt = vi.fn()
    const onClose = vi.fn()
    const ref = mount('edited body', 'disk body', onClose)

    act(() => ref.current!.requestClose({ beforePrompt }))

    expect(await screen.findByText('Discard unsaved changes?')).toBeInTheDocument()
    expect(beforePrompt).toHaveBeenCalledOnce()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('does not run when the close needs no answer', () => {
    const beforePrompt = vi.fn()
    const onClose = vi.fn()
    const ref = mount('same body', 'same body', onClose)

    act(() => ref.current!.requestClose({ beforePrompt }))

    expect(onClose).toHaveBeenCalledOnce()
    expect(beforePrompt).not.toHaveBeenCalled()
  })
})
