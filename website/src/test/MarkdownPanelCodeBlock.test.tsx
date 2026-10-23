/**
 * Fenced code blocks in the markdown preview, through the real panel: Cmd+F finds
 * text inside them, and a selection made inside one can be commented on.
 *
 * Pierre draws a block into an open shadow root, which the find's text walk and
 * the document's Selection both stop at. `PierreCode` is stubbed here with a host
 * shaped like the one Pierre renders (see `pierreDom`), so the panel, `CodeBlock`,
 * the find hook and `SelectionToolbar` are all the real thing.
 *
 * `Highlight` and `CSS.highlights` are stubbed BEFORE the panel is imported,
 * because it captures both into module-level constants at load time.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createElement, forwardRef, useImperativeHandle } from 'react'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PierreEditorHandle } from '../pierre'
import { installHighlightApiStub, paintedText, registeredRanges } from './highlightApiStub'
import { dragInCode, dragInPlainCode, fillCodeBlock, tokens } from './pierreDom'

installHighlightApiStub()

const pierreMockState = vi.hoisted(() => ({ plainWholeFile: false }))

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreEditor: forwardRef<PierreEditorHandle, { file: { contents: string } }>(
    function PierreEditorStub({ file }, ref) {
      useImperativeHandle(ref, () => ({ jumpToLine: () => {}, focus: () => {} }), [])
      return <div data-testid="pierre-editor" data-value={file.contents} />
    },
  ),
  PierreCode: ({ file, scrollClassName }: { file: { contents: string }; scrollClassName?: string }) => {
    if (pierreMockState.plainWholeFile && scrollClassName) {
      return <div className={scrollClassName}><pre className="pierre-plain" data-testid="plain-code-file"><code>{file.contents}</code></pre></div>
    }
    return createElement('diffs-container', {
      'data-testid': 'code-host',
      ref: (host: HTMLElement | null) => { if (host && !host.shadowRoot) fillCodeBlock(host, file.contents.split('\n').map(tokens)) },
    })
  },
  // A diff row is drawn in a shadow root too; a fixed line is enough to drag over.
  PierreFilePair: () => createElement('diffs-container', {
    'data-testid': 'diff-host',
    ref: (host: HTMLElement | null) => { if (host && !host.shadowRoot) fillCodeBlock(host, [tokens('const answer = 42')]) },
  }),
}))

vi.mock('../api/client', () => ({
  api: {
    artifacts: vi.fn(),
    artifact: vi.fn(),
    createArtifact: vi.fn(),
    updateArtifact: vi.fn(),
    setArtifactPinned: vi.fn(),
    revealPath: vi.fn(),
    fileDiff: vi.fn(),
  },
}))

const { api } = await import('../api/client')
const { default: MarkdownPanel } = await import('../components/MarkdownPanel')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter>
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  </MemoryRouter>
)

beforeEach(() => {
  pierreMockState.plainWholeFile = false
  vi.clearAllMocks()
  qc.clear()
  localStorage.clear()
  sessionStorage.clear()
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: async () => ({ enabled: false, supported_formats: [] }),
    text: async () => '',
  })))
  vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [] } as never)
  vi.mocked(api.artifact).mockResolvedValue({ live_dirty: false, pinned: false } as never)
  vi.mocked(api.fileDiff).mockResolvedValue({ diff: '', original: '', status: 'clean' } as never)
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

const FILE = '/tmp/notes.md'
const BODY = '# Title\n\nRun it first, with care.\n\n```bash\nmycli rm NAME --force\n```\n'

/** `initialDiffMode: undefined` leaves the auto-diff effect inert, so a click on the diff toggle cannot race back to preview. */
function mountPanel(content = BODY, onSubmitComments = vi.fn(), extra: { initialDiffMode?: boolean } = {}) {
  render(
    <MarkdownPanel embedded filePath={FILE} content={content} onContentChange={vi.fn()} onSave={vi.fn(async () => {})}
      onClose={vi.fn()} onSubmitComments={onSubmitComments} initialDiffMode={false} {...extra} />,
    { wrapper },
  )
  return onSubmitComments
}

describe('MarkdownPanel — find in a fenced code block', () => {
  async function openFind() {
    mountPanel()
    await screen.findByTestId('code-host')
    fireEvent.keyDown(document, { key: 'f', metaKey: true })
    return screen.findByLabelText('Find in document')
  }

  it('finds a phrase that crosses the tokens of a line, and paints exactly that text', async () => {
    fireEvent.change(await openFind(), { target: { value: 'rm NAME' } })
    expect(await screen.findByText('1 of 1')).toBeInTheDocument()
    expect(paintedText('mc-find-current')).toEqual(['rm NAME'])
    expect(registeredRanges('mc-find-current')[0].startContainer.getRootNode()).toBeInstanceOf(ShadowRoot)
  })

  it('counts the line numbers beside the code as no match', async () => {
    fireEvent.change(await openFind(), { target: { value: '1' } })
    expect(await screen.findByText('No results')).toBeInTheDocument()
  })
})

describe('MarkdownPanel — commenting on a fenced code block', () => {
  /** Drag over `mycli rm NAME` in the block; the comment box opens on its own (type-first). */
  async function selectInCode() {
    const host = await screen.findByTestId('code-host')
    dragInCode(host)
    return screen.findByLabelText('Comment on the selected text')
  }

  it('anchors the comment to the selected code, with the source line it was found on', async () => {
    const onSubmitComments = mountPanel()
    const box = await selectInCode()
    // The open box paints the selection, one range per text node (a token each).
    expect(paintedText('mc-annotate').join('')).toBe('mycli rm NAME')
    expect(registeredRanges('mc-annotate').every(r => r.startContainer.getRootNode() instanceof ShadowRoot)).toBe(true)
    fireEvent.change(box, { target: { value: 'why force?' } })
    fireEvent.click(screen.getByLabelText('Add comment'))
    expect(await screen.findByText('why force?')).toBeInTheDocument()
    const stored = JSON.parse(localStorage.getItem('mc-comment-drafts') || '{}')
    expect(stored[FILE]).toHaveLength(1)
    expect(stored[FILE][0]).toMatchObject({ anchor: 'mycli rm NAME', text: 'why force?', line: 6, column: 1, inCode: true })
    expect(stored[FILE][0]).not.toHaveProperty('startOffset')
    expect(onSubmitComments).not.toHaveBeenCalled()
  })

  it('finds the line of a selection that includes the space after the text', async () => {
    mountPanel()
    const host = await screen.findByTestId('code-host')
    dragInCode(host, { text: 'rm NAME --force ' })
    fireEvent.change(await screen.findByLabelText('Comment on the selected text'), { target: { value: 'padded' } })
    fireEvent.click(screen.getByLabelText('Add comment'))
    await screen.findByText('padded')
    const stored = JSON.parse(localStorage.getItem('mc-comment-drafts') || '{}')
    expect(stored[FILE][0]).toMatchObject({ anchor: 'rm NAME --force', line: 6, column: 7 })
  })

  /** Mounts `content`, drags over `text` in its `block`th block and adds a comment; resolves to the comment as saved. */
  async function commentOn(content: string, text: string, block = 0) {
    mountPanel(content)
    dragInCode((await screen.findAllByTestId('code-host'))[block], { text })
    fireEvent.change(await screen.findByLabelText('Comment on the selected text'), { target: { value: 'note' } })
    fireEvent.click(screen.getByLabelText('Add comment'))
    await screen.findByText('note')
    return JSON.parse(localStorage.getItem('mc-comment-drafts') || '{}')[FILE][0]
  }

  it('saves the column of the first visible character when the selection starts on the indentation', async () => {
    const saved = await commentOn('```bash\nif ok; then\n  mycli rm NAME\nfi\n```\n', '  mycli rm NAME')
    expect(saved).toMatchObject({ anchor: 'mycli rm NAME', line: 3, column: 3 })
  })

  it('finds the line of a selection across rows in a file with CRLF line endings', async () => {
    const saved = await commentOn('```bash\r\nif ok; then\r\n  mycli rm NAME\r\n  mycli rm OTHER\r\nfi\r\n```\r\n', '  mycli rm NAME\n  mycli rm OTHER')
    expect(saved).toMatchObject({ anchor: 'mycli rm NAME\n  mycli rm OTHER', line: 3, column: 3 })
  })

  it('puts no line on a nested block\'s selection that starts on a newline when another block opens with the same words', async () => {
    const saved = await commentOn('1. Clean up:\n   ```bash\n   ls\n   mycli rm NAME\n   ```\n\n```bash\nmycli rm NAME --force\n```\n', '\nmycli rm NAME')
    expect(saved).toMatchObject({ anchor: 'mycli rm NAME', inCode: true })
    expect(saved).not.toHaveProperty('line')
    expect(saved).not.toHaveProperty('column')
  })

  it('puts no line on text that occurs twice in the file, rather than the first occurrence’s', async () => {
    mountPanel('Run mycli rm NAME by hand.\n\n```bash\nmycli rm NAME --force\n```\n')
    fireEvent.change(await selectInCode(), { target: { value: 'which one?' } })
    fireEvent.click(screen.getByLabelText('Add comment'))
    await screen.findByText('which one?')
    const [saved] = JSON.parse(localStorage.getItem('mc-comment-drafts') || '{}')[FILE]
    expect(saved).toMatchObject({ anchor: 'mycli rm NAME', inCode: true })
    expect(saved).not.toHaveProperty('line')
    expect(saved).not.toHaveProperty('column')
  })

  it('quotes the commented line of a code file, not the line above it', async () => {
    const onSubmitComments = vi.fn()
    const props = { embedded: true, onContentChange: vi.fn(), onSave: vi.fn(async () => {}), onClose: vi.fn(), onSubmitComments, initialDiffMode: false }
    // A code file opens in the editor; the rail re-targeting a tab in preview keeps the preview.
    const { rerender } = render(<MarkdownPanel {...props} filePath={FILE} content={BODY} />, { wrapper })
    await screen.findByTestId('code-host')
    rerender(<MarkdownPanel {...props} filePath="/tmp/notes.ts" content={'const a = 1\nconst b = pick(a)\nconst c = 3\n'} />)
    dragInCode(await screen.findByTestId('code-host'), { text: 'pick(a)' })
    fireEvent.change(await screen.findByLabelText('Comment on the selected text'), { target: { value: 'note' } })
    fireEvent.click(screen.getByLabelText('Add comment'))
    await screen.findByText('note')
    await act(async () => { fireEvent.click(screen.getByText(/Submit All/)) })
    expect(onSubmitComments).toHaveBeenCalledOnce()
    expect(onSubmitComments.mock.calls[0][0]).toContain('1. (line 2, col 11, "pick(a)" in "const b = pick(a)"): "note"')
  })

  it('quotes the commented line of a code file from its plain fallback', async () => {
    pierreMockState.plainWholeFile = true
    const onSubmitComments = vi.fn()
    const props = { embedded: true, onContentChange: vi.fn(), onSave: vi.fn(async () => {}), onClose: vi.fn(), onSubmitComments, initialDiffMode: false }
    const { rerender } = render(<MarkdownPanel {...props} filePath={FILE} content={BODY} />, { wrapper })
    await screen.findByTestId('code-host')
    rerender(<MarkdownPanel {...props} filePath="/tmp/notes.ts" content={'const a = 1\nconst b = pick(a)\nconst c = 3\n'} />)
    dragInPlainCode(await screen.findByTestId('plain-code-file'), { lineIndex: 1, start: 10, end: 17 })
    fireEvent.change(await screen.findByLabelText('Comment on the selected text'), { target: { value: 'note' } })
    fireEvent.click(screen.getByLabelText('Add comment'))
    await screen.findByText('note')
    await act(async () => { fireEvent.click(screen.getByText(/Submit All/)) })
    expect(onSubmitComments).toHaveBeenCalledOnce()
    expect(onSubmitComments.mock.calls[0][0]).toContain('1. (line 2, col 11, "pick(a)" in "const b = pick(a)"): "note"')
  })

  it('underlines the commented code, not the same words in the prose', async () => {
    mountPanel('Run mycli rm NAME by hand.\n\n```bash\nmycli rm NAME --force\n```\n')
    fireEvent.change(await selectInCode(), { target: { value: 'same words twice' } })
    fireEvent.click(screen.getByLabelText('Add comment'))
    await screen.findByText('same words twice')
    await waitFor(() => expect(paintedText('mc-comment')).toEqual(['mycli rm NAME']))
    expect(registeredRanges('mc-comment')[0].startContainer.getRootNode()).toBe(screen.getByTestId('code-host').shadowRoot)
  })

  it('underlines the block the comment was made in when another block has the same words', async () => {
    const saved = await commentOn('```bash\nmycli rm NAME --force\n```\n\n```bash\nmycli rm NAME --force\n```\n', 'mycli rm NAME', 1)
    expect(saved).toHaveProperty('codePosition')
    const second = screen.getAllByTestId('code-host')[1]
    await waitFor(() => expect(registeredRanges('mc-comment').map(r => r.startContainer.getRootNode())).toEqual([second.shadowRoot]))
    expect(paintedText('mc-comment')).toEqual(['mycli rm NAME'])
  })

  it('underlines a code comment saved before it had a position when its words occur once in the code', async () => {
    localStorage.setItem('mc-comment-drafts', JSON.stringify({ [FILE]: [{ id: 'c1', anchor: 'rm NAME', text: 'old note', inCode: true }] }))
    mountPanel()
    await screen.findByTestId('code-host')
    await waitFor(() => expect(paintedText('mc-comment')).toEqual(['rm NAME']))
  })

  it('underlines the code again when the block is redrawn', async () => {
    await commentOn(BODY, 'mycli rm NAME')
    const host = screen.getByTestId('code-host')
    await waitFor(() => expect(paintedText('mc-comment')).toEqual(['mycli rm NAME']))
    const before = registeredRanges('mc-comment')[0]
    await act(async () => { fillCodeBlock(host, [tokens('mycli rm NAME --force')]) })
    await waitFor(() => expect(registeredRanges('mc-comment')[0]).not.toBe(before))
    expect(registeredRanges('mc-comment')[0].startContainer.isConnected).toBe(true)
    expect(paintedText('mc-comment')).toEqual(['mycli rm NAME'])
  })

  it('shows the comment text when the pointer rests on the commented code', async () => {
    await commentOn(BODY, 'mycli rm NAME')
    await waitFor(() => expect(paintedText('mc-comment')).toEqual(['mycli rm NAME']))
    const painted = registeredRanges('mc-comment')[0]
    // The caret stops at the host; the painted text's box is what is hit.
    painted.getClientRects = () => [new DOMRect(10, 10, 80, 16)] as unknown as DOMRectList
    const scroller = document.querySelector('.msg-content')!.parentElement as HTMLElement
    await act(async () => { fireEvent.mouseMove(scroller, { clientX: 20, clientY: 15 }) })
    expect(document.querySelector('.mc-comment-tooltip')).toHaveTextContent('note')
  })

  it.each([
    { text: 'mycli rm NAME', cleared: false },
    { text: '', cleared: true },
  ])('clicking a code comment with selection "$text" clears it: $cleared', async ({ text, cleared }) => {
    await commentOn(BODY, 'mycli rm NAME')
    await waitFor(() => expect(paintedText('mc-comment')).toEqual(['mycli rm NAME']))
    const painted = registeredRanges('mc-comment')[0]
    painted.getClientRects = () => [new DOMRect(10, 10, 80, 16)] as unknown as DOMRectList
    const removeAllRanges = vi.fn()
    vi.spyOn(window, 'getSelection').mockReturnValue({
      isCollapsed: true,
      toString: () => text,
      removeAllRanges,
    } as unknown as Selection)
    const scroller = document.querySelector('.msg-content')!.parentElement as HTMLElement

    const allowed = fireEvent.click(scroller, { clientX: 20, clientY: 15 })

    expect(removeAllRanges).toHaveBeenCalledTimes(cleared ? 1 : 0)
    expect(allowed).toBe(!cleared)
  })

  it('underlines a prose comment in the prose, as before', async () => {
    localStorage.setItem('mc-comment-drafts', JSON.stringify({ [FILE]: [{ id: 'p1', anchor: 'with care', text: 'prose note' }] }))
    mountPanel()
    await screen.findByTestId('code-host')
    await waitFor(() => expect(paintedText('mc-comment')).toEqual(['with care']))
    expect(registeredRanges('mc-comment')[0].startContainer.getRootNode()).toBe(document)
  })
})

describe('MarkdownPanel — a drag in the diff view', () => {
  /** The diff view has no preview root, so a comment on it could only be quote-only; the toolbar must not open. */
  async function openDiff() {
    vi.mocked(api.fileDiff).mockResolvedValue({ diff: 'x', original: 'old\n', status: 'clean' } as never)
    mountPanel(BODY, vi.fn(), { initialDiffMode: undefined })
    await waitFor(() => expect(api.fileDiff).toHaveBeenCalled())
    fireEvent.click(screen.getAllByLabelText('Toggle diff view')[0])
    await screen.findByTestId('diff-host')
  }

  function dragAndLook() {
    const host = screen.getByTestId('diff-host')
    vi.useFakeTimers()
    const selection = dragInCode(host)
    act(() => { vi.advanceTimersByTime(60) })
    expect(screen.queryByLabelText('Comment on the selected text')).toBeNull()
    expect(selection.getComposedRanges).not.toHaveBeenCalled()
  }

  it('opens no comment box in the side panel', async () => {
    await openDiff()
    dragAndLook()
  })

  it('opens no comment box in full screen', async () => {
    await openDiff()
    fireEvent.click(screen.getAllByTestId('markdown-panel-more-options')[0])
    fireEvent.click(screen.getByText('Full screen'))
    await screen.findByRole('dialog')
    await screen.findByTestId('diff-host')
    dragAndLook()
  })
})
