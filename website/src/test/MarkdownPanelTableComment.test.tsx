/**
 * A comment on text that runs across two table cells gets its underline (#25490).
 *
 * A real browser's `Selection.toString()` puts a tab between table cells (and a
 * newline between rows and blocks); the highlighter searches the preview's
 * `textContent`, which has none of those. happy-dom's `Selection.toString()`
 * returns the range's own text, so each test stubs the string a browser returns.
 * The comment keeps the browser's text as its quote, so the reader and the agent
 * see the gap between the cells; only the underline search uses the rendered text.
 *
 * `Highlight` and `CSS.highlights` are stubbed BEFORE the panel is imported,
 * because it captures both into module-level constants at load time.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { forwardRef, useImperativeHandle } from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PierreEditorHandle } from '../pierre'
import { installHighlightApiStub, paintedText } from './highlightApiStub'

installHighlightApiStub()

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
  vi.restoreAllMocks()
})

const TABLE = '# Title\n\n| Name | Role |\n| --- | --- |\n| alpha | beta |\n'

function mountPanel(content: string) {
  render(
    <MarkdownPanel embedded filePath="/tmp/notes.md" content={content} onContentChange={vi.fn()} onSave={vi.fn(async () => {})}
      onClose={vi.fn()} onSubmitComments={vi.fn()} initialDiffMode={false} />,
    { wrapper },
  )
}

/** Select from `startNode[startOffset]` to `endNode[endOffset]`, with the selection
 *  reading back as `browserText`, the string a real browser gives for it. */
function selectAcross(startNode: Text, startOffset: number, endNode: Text, endOffset: number, browserText: string) {
  const range = document.createRange()
  range.setStart(startNode, startOffset)
  range.setEnd(endNode, endOffset)
  const sel = window.getSelection()!
  sel.removeAllRanges()
  sel.addRange(range)
  vi.spyOn(sel, 'toString').mockReturnValue(browserText)
  fireEvent.mouseUp(document)
}

/** The comments the panel saved for the file, as it persists them. */
function savedComments(): Record<string, unknown>[] {
  return JSON.parse(localStorage.getItem('mc-comment-drafts') || '{}')['/tmp/notes.md'] ?? []
}

async function addComment(text: string) {
  fireEvent.change(await screen.findByLabelText('Comment on the selected text'), { target: { value: text } })
  fireEvent.click(screen.getByLabelText('Add comment'))
  await screen.findByText(text)
}

describe('MarkdownPanel — a comment across table cells', () => {
  it('underlines text selected from one cell into the next', async () => {
    mountPanel(TABLE)
    const alpha = (await screen.findByText('alpha')).firstChild as Text
    const beta = screen.getByText('beta').firstChild as Text
    // "pha" in the first cell through "be" in the second; a browser joins them with a tab.
    selectAcross(alpha, 2, beta, 2, 'pha\tbe')
    await addComment('across cells')
    await waitFor(() => expect(paintedText('mc-comment')).toEqual(['phabe']))
    // The quote the list shows and the agent is sent keeps the gap between the cells.
    expect(savedComments()[0]).toMatchObject({ anchor: 'pha\tbe', renderedQuote: 'phabe' })
  })

  it('still underlines text inside a single cell', async () => {
    mountPanel(TABLE)
    const beta = (await screen.findByText('beta')).firstChild as Text
    selectAcross(beta, 1, beta, 3, 'et')
    await addComment('one cell')
    await waitFor(() => expect(paintedText('mc-comment')).toEqual(['et']))
    expect(savedComments()[0].anchor).toBe('et')
    expect(savedComments()[0]).not.toHaveProperty('renderedQuote')
  })
})
