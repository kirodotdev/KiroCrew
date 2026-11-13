/**
 * Comments across table cells in the file viewer, through the real panel and
 * `SelectionToolbar`: the cases #25585 left open after it gave a comment a
 * `renderedQuote` to search by.
 *
 * A browser's `Selection.toString()` writes whitespace the preview's text nodes
 * don't hold: a tab between table cells, a newline between rows. It also leaves
 * out the table's Copy buttons, which can't be selected. happy-dom's returns
 * the range's own text, so each test stubs it with the string Chrome 155
 * returned for the same selection over this same document in the dashboard.
 *
 * `Highlight` and `CSS.highlights` are stubbed BEFORE the panel is imported,
 * because it captures both into module-level constants at load time.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { forwardRef, useImperativeHandle } from 'react'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PierreEditorHandle } from '../pierre'
import { installHighlightApiStub, paintedText, registeredRanges } from './highlightApiStub'

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

const FILE = '/tmp/table-comments.md'
// Line numbers in the tests are this document's: the header row is line 8, the
// first body row line 10, the body row of the table below the fence line 23.
const BODY = [
  '# Table comment fixture',
  '',
  'Intro paragraph one with a soft',
  'line break in the middle.',
  '',
  'Second paragraph.',
  '',
  '| Change | Why it matters | Owner |',
  '|---|---|---|',
  '| Swarm mode on more of my work | More review per change | Ryan |',
  '| Cells with `inline code` and **bold** | A line<br>break inside a cell | |',
  '| Non&nbsp;breaking space | Escaped \\| pipe | Done |',
  '| Swarm mode on more of my work | More review per change | Ryan |',
  '',
  'After-table paragraph.',
  '',
  '```bash',
  'echo fence',
  '```',
  '',
  '| After fence | Second |',
  '|---|---|',
  '| alpha | beta |',
  '',
].join('\n')

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
  window.getSelection()?.removeAllRanges()
  vi.restoreAllMocks()
  delete (document as { caretRangeFromPoint?: unknown }).caretRangeFromPoint
})

function panel(content: string, onSubmitComments = vi.fn()) {
  return <MarkdownPanel embedded filePath={FILE} content={content} onContentChange={vi.fn()} onSave={vi.fn(async () => {})}
    onClose={vi.fn()} onSubmitComments={onSubmitComments} initialDiffMode={false} />
}

function mount(content = BODY, onSubmitComments = vi.fn()) {
  const view = render(panel(content, onSubmitComments), { wrapper })
  return { ...view, onSubmitComments }
}

/** The rendered markdown body, and its text nodes with each one's offset in the joined text. */
function preview() {
  const root = document.querySelector('.msg-content') as HTMLElement
  const nodes: { node: Text; start: number }[] = []
  let text = ''
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    nodes.push({ node: n as Text, start: text.length })
    text += (n as Text).data
  }
  return { root, nodes, text }
}

/** The `k`th occurrence of `needle` in the preview's text. */
function nth(text: string, needle: string, k = 0): number {
  let at = -1
  for (let i = 0; i <= k; i++) at = text.indexOf(needle, at + 1)
  if (at < 0) throw new Error(`"${needle}" #${k} is not in the preview`)
  return at
}

/** A Range from offset `start` to offset `end` of the preview's text, each end inside the node holding that character. */
function rangeOver(start: number, end: number): Range {
  const { nodes } = preview()
  const at = (off: number) => {
    const hit = nodes.find(n => off < n.start + n.node.data.length)!
    return { node: hit.node, offset: off - hit.start }
  }
  const s = at(start)
  const e = at(end - 1)
  const r = document.createRange()
  r.setStart(s.node, s.offset)
  r.setEnd(e.node, e.offset + 1)
  return r
}

/** Make the document's selection report `browserText` from `toString()`, as Chrome does for the same range. */
function selectionReads(browserText: string) {
  const proto = Object.getPrototypeOf(window.getSelection()!) as Selection
  vi.spyOn(proto, 'toString').mockImplementation(function (this: Selection) {
    return this.rangeCount && !this.isCollapsed ? browserText : ''
  })
}

/** Select from the `fromK`th `from` to the end of the `toK`th `to`, as a reader would, and open the composer. */
async function selectAcross(from: string, to: string, browserText: string, fromK = 0, toK = 0) {
  await screen.findByText(/After-table paragraph/)
  const { text } = preview()
  const range = rangeOver(nth(text, from, fromK), nth(text, to, toK) + to.length)
  selectionReads(browserText)
  const sel = window.getSelection()!
  sel.removeAllRanges()
  sel.addRange(range)
  fireEvent.mouseUp(document)
  return screen.findByLabelText('Comment on the selected text')
}

async function addComment(box: HTMLElement, text: string) {
  fireEvent.change(box, { target: { value: text } })
  fireEvent.click(screen.getByLabelText('Add comment'))
  await waitFor(() => expect(document.querySelector('[data-comment-id]')).not.toBeNull())
}

function savedComments(): { anchor: string; line?: number; startOffset?: number; renderedQuote?: string }[] {
  return JSON.parse(localStorage.getItem('mc-comment-drafts') || '{}')[FILE] ?? []
}

describe('MarkdownPanel: table comment cases #25585 left open', () => {
  it('paints a comment that runs out of a table into the next paragraph around the table\'s buttons', async () => {
    mount()
    await addComment(await selectAcross('per change', 'After-table', 'per change\tRyan\nAfter-table', 1), 'check this')
    // The range's text, which the comment is found by, holds the Copy buttons
    // between the cells and the paragraph; the reader couldn't select them.
    expect(savedComments()[0].renderedQuote).toContain('Copy Markdown')
    await waitFor(() => expect(paintedText('mc-comment')).toEqual(['per changeRyan', 'After-table']))
  })

  it('highlights a comment saved without renderedQuote, whose quote holds the tab', async () => {
    const first = mount()
    await screen.findByText(/After-table paragraph/)
    const at = nth(preview().text, 'of my work', 1)
    const outOfTable = nth(preview().text, 'per change', 1)
    first.unmount()
    localStorage.setItem('mc-comment-drafts', JSON.stringify({
      [FILE]: [
        { id: 'with-offset', anchor: 'of my work\tMore review', text: 'later row', line: 13, column: 22, startOffset: at },
        { id: 'no-offset', anchor: 'Why it matters\tOwner', text: 'header' },
        // Two rows hold these cells, and nothing saved says which one was selected.
        { id: 'ambiguous', anchor: 'of my work\tMore review', text: 'which row?' },
        // Chrome's string leaves out the Copy buttons, which the page text holds.
        { id: 'out-of-table', anchor: 'per change\tRyan\nAfter-table', text: 'into the paragraph', line: 13, startOffset: outOfTable },
      ],
    }))
    const { unmount } = mount()
    await waitFor(() => expect(paintedText('mc-comment')).toEqual(['of my workMore review', 'Why it mattersOwner', 'per changeRyan', 'After-table']))
    // The comment with an offset lands on the copy it was made on, the last row.
    expect(registeredRanges('mc-comment')[0].startContainer.parentElement!.closest('tr'))
      .toBe(document.querySelectorAll('.msg-content tbody tr')[3])
    unmount()
  })

  it('shows a comment over every cell it covers, and not over the cell before it', async () => {
    mount()
    await addComment(await selectAcross('More review per change', 'Ryan', 'More review per change\tRyan'), 'needs an owner')
    await waitFor(() => expect(paintedText('mc-comment')).toEqual(['More review per changeRyan']))
    // The comment starts where the first cell's text ends, so a caret at the
    // end of that cell must not count as on the comment.
    const firstCell = document.querySelector('.msg-content tbody tr td')!.firstChild as Text
    const ownerCell = document.querySelectorAll('.msg-content tbody tr')[0].querySelectorAll('td')[2].firstChild as Text
    let caretAt: { node: Text; offset: number } = { node: firstCell, offset: firstCell.data.length }
    Object.defineProperty(document, 'caretRangeFromPoint', {
      configurable: true,
      value: () => {
        const caret = document.createRange()
        caret.setStart(caretAt.node, caretAt.offset)
        caret.collapse(true)
        return caret
      },
    })
    const scrollRoot = document.querySelector('.msg-content')!.parentElement as HTMLElement
    await act(async () => { fireEvent.mouseMove(scrollRoot, { clientX: 20, clientY: 20 }) })
    expect(document.querySelector('.mc-comment-tooltip')).toBeNull()
    caretAt = { node: ownerCell, offset: 2 }
    await act(async () => { fireEvent.mouseMove(scrollRoot, { clientX: 30, clientY: 20 }) })
    expect(document.querySelector('.mc-comment-tooltip')).toHaveTextContent('needs an owner')
  })

  it('quotes a Firefox cell selection, one range per cell, from its first cell, where its line points', async () => {
    mount()
    await screen.findByText(/After-table paragraph/)
    const { text } = preview()
    const first = rangeOver(nth(text, 'More review per change'), nth(text, 'More review per change') + 22)
    const second = rangeOver(nth(text, 'Ryan'), nth(text, 'Ryan') + 4)
    // Firefox's string for two selected cells joins them; range 0 is the first cell.
    selectionReads('More review per change\tRyan')
    const proto = Object.getPrototypeOf(window.getSelection()!) as Selection
    vi.spyOn(proto, 'rangeCount', 'get').mockReturnValue(2)
    const getRangeAt = vi.spyOn(proto, 'getRangeAt').mockImplementation((i: number) => (i === 0 ? first : second))
    const sel = window.getSelection()!
    sel.removeAllRanges()
    sel.addRange(first)
    fireEvent.mouseUp(document)
    // The composer already shows range 0's text.
    const box = await screen.findByLabelText('Comment on the selected text')
    await addComment(box, 'first cell only')
    getRangeAt.mockRestore()
    expect(savedComments()[0]).toMatchObject({ anchor: 'More review per change', line: 10 })
    expect(savedComments()[0]).not.toHaveProperty('renderedQuote')
    await waitFor(() => expect(paintedText('mc-comment')).toEqual(['More review per change']))
  })
})
