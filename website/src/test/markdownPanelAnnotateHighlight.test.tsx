/**
 * The annotation selection highlight paints through the CSS Custom Highlight
 * API, never by mutating the react-markdown preview DOM.
 *
 * The preview is React-reconciled: wrapping selected text in <mark> elements
 * splits React-owned text nodes, and unwrapping them merges nodes back, so a
 * later commit that rewrites that text throws NotFoundError or updates a
 * detached node. Ranges registered under `mc-annotate` live outside the DOM,
 * so a content re-render simply stops painting them. These tests pin all three
 * halves of that contract: the registry receives the ranges, the preview DOM
 * is untouched (no <mark>, text node count unchanged), and a re-render with
 * changed content completes while the highlight is live.
 *
 * `Highlight` and `CSS.highlights` are stubbed BEFORE the module is imported,
 * because MarkdownPanel captures both into module-level constants at load
 * time. happy-dom ships neither, so without the stub every highlight path is
 * unreachable.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { forwardRef, useImperativeHandle } from 'react'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PierreEditorHandle } from '../pierre'

// ── CSS Custom Highlight API stub (must precede the dynamic import) ──────────
const highlightRegistry = new Map<string, Range[]>()
class StubHighlight {
  readonly ranges: Range[]
  constructor(...ranges: Range[]) { this.ranges = ranges }
}
vi.stubGlobal('Highlight', StubHighlight)
vi.stubGlobal('CSS', {
  highlights: {
    set: (name: string, hl: StubHighlight) => { highlightRegistry.set(name, hl.ranges) },
    delete: (name: string) => highlightRegistry.delete(name),
  },
  escape: (s: string) => s,
  supports: () => false,
})

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreEditor: forwardRef<PierreEditorHandle, { file: { contents: string } }>(
    function PierreEditorStub({ file }, ref) {
      useImperativeHandle(ref, () => ({ jumpToLine: () => {}, focus: () => {} }), [])
      return <div data-testid="pierre-editor" data-value={file.contents} />
    },
  ),
  PierreCode: ({ file }: { file: { contents: string } }) => (
    <div data-testid="pierre-code" data-value={file.contents} />
  ),
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
  highlightRegistry.clear()
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

const BODY = '# Title\n\nalpha beta gamma\n'

function panelProps(content: string) {
  return {
    embedded: true as const,
    filePath: '/tmp/notes.md',
    content,
    onContentChange: vi.fn(),
    onSave: vi.fn(async () => {}),
    onClose: vi.fn(),
    onSubmitComments: vi.fn(),
    initialDiffMode: false,
  }
}

/** Count the text nodes under `root` (what <mark> wrapping used to split). */
function textNodeCount(root: Node): number {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  let n = 0
  while (walker.nextNode()) n++
  return n
}

function selectWordInParagraph(para: HTMLElement, word: string) {
  const textNode = para.firstChild as Text
  const start = textNode.data.indexOf(word)
  const range = document.createRange()
  range.setStart(textNode, start)
  range.setEnd(textNode, start + word.length)
  const sel = window.getSelection()!
  sel.removeAllRanges()
  sel.addRange(range)
  fireEvent.mouseUp(document)
}

/** Select `word` inside the rendered preview paragraph; the annotation input
 *  opens on its own (type-first). Returns the paragraph element. */
async function selectInPreview(word: string) {
  const para = await screen.findByText(/alpha beta gamma/)
  selectWordInParagraph(para, word)
  await screen.findByLabelText('Comment on the selected text')
  return para
}

function TwoPanels({ active, showB = true }: { active: 'a' | 'b'; showB?: boolean }) {
  return (
    <>
      <div data-testid="tab-a">
        <MarkdownPanel {...panelProps('# Alpha\n\nalpha beta gamma\n')} active={active === 'a'} filePath="/tmp/a.md" />
      </div>
      {showB && (
        <div data-testid="tab-b">
          <MarkdownPanel {...panelProps('# Bravo\n\ndelta epsilon zeta\n')} active={active === 'b'} filePath="/tmp/b.md" />
        </div>
      )}
    </>
  )
}

describe('MarkdownPanel — annotation selection highlight', () => {
  it('preserves each mounted file tab highlight when another composer opens and closes', async () => {
    const { rerender } = render(<TwoPanels active="a" showB={false} />, { wrapper })
    const tabA = screen.getByTestId('tab-a')
    selectWordInParagraph(await within(tabA).findByText(/alpha beta gamma/), 'beta')
    const boxA = await screen.findByLabelText('Comment on the selected text')
    expect(highlightRegistry.get('mc-annotate')?.map(r => r.toString())).toEqual(['beta'])

    rerender(<TwoPanels active="b" />)
    // Inactive tabs keep their ranges, not their portaled input. Wait for A's
    // exit animation instead of asserting on its brief overlap with B's input.
    await waitFor(() => expect(boxA).not.toBeInTheDocument())
    const tabB = screen.getByTestId('tab-b')
    selectWordInParagraph(await within(tabB).findByText(/delta epsilon zeta/), 'epsilon')
    const boxB = await screen.findByLabelText('Comment on the selected text')
    expect(highlightRegistry.get('mc-annotate')?.map(r => r.toString()).sort()).toEqual(['beta', 'epsilon'])

    fireEvent.keyDown(boxB, { key: 'Escape' })
    await waitFor(() => expect(screen.queryAllByLabelText('Comment on the selected text')).toHaveLength(0))
    expect(highlightRegistry.get('mc-annotate')?.map(r => r.toString())).toEqual(['beta'])

    rerender(<TwoPanels active="a" />)
    await screen.findByLabelText('Comment on the selected text')
    expect(highlightRegistry.get('mc-annotate')?.map(r => r.toString())).toEqual(['beta'])
  })

  it('removes only the unmounted file tab ranges', async () => {
    const { rerender } = render(<TwoPanels active="a" showB={false} />, { wrapper })
    const tabA = screen.getByTestId('tab-a')
    selectWordInParagraph(await within(tabA).findByText(/alpha beta gamma/), 'beta')
    const boxA = await screen.findByLabelText('Comment on the selected text')

    rerender(<TwoPanels active="b" />)
    await waitFor(() => expect(boxA).not.toBeInTheDocument())
    const tabB = screen.getByTestId('tab-b')
    selectWordInParagraph(await within(tabB).findByText(/delta epsilon zeta/), 'epsilon')
    await screen.findByLabelText('Comment on the selected text')
    expect(highlightRegistry.get('mc-annotate')?.map(r => r.toString()).sort()).toEqual(['beta', 'epsilon'])

    rerender(<TwoPanels active="a" showB={false} />)
    await waitFor(() => expect(highlightRegistry.get('mc-annotate')?.map(r => r.toString())).toEqual(['beta']))
  })

  it('registers the selection under mc-annotate without touching the preview DOM', async () => {
    render(<MarkdownPanel {...panelProps(BODY)} />, { wrapper })
    const para = await screen.findByText(/alpha beta gamma/)
    const nodesBefore = textNodeCount(para)

    await selectInPreview('beta')

    const ranges = highlightRegistry.get('mc-annotate')
    expect(ranges).toBeDefined()
    expect(ranges!.length).toBe(1)
    expect(ranges![0].toString()).toBe('beta')
    // The DOM the highlight paints over is exactly the DOM React rendered.
    expect(document.querySelector('mark')).toBeNull()
    expect(textNodeCount(para)).toBe(nodesBefore)
  })

  it('clearing the selection removes the mc-annotate entry', async () => {
    render(<MarkdownPanel {...panelProps(BODY)} />, { wrapper })
    const box = await (async () => { await selectInPreview('beta'); return screen.getByLabelText('Comment on the selected text') })()
    expect(highlightRegistry.has('mc-annotate')).toBe(true)

    fireEvent.keyDown(box, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByLabelText('Comment on the selected text')).toBeNull())
    expect(highlightRegistry.has('mc-annotate')).toBe(false)
    expect(document.querySelector('mark')).toBeNull()
  })

  it('a content re-render completes while the highlight is live', async () => {
    const props = panelProps(BODY)
    const { rerender } = render(<MarkdownPanel {...props} />, { wrapper })
    await selectInPreview('beta')
    expect(highlightRegistry.has('mc-annotate')).toBe(true)

    // React reconciles the preview against fibers that still own every text
    // node, because the highlight never split or wrapped any of them.
    expect(() => {
      rerender(<MarkdownPanel {...props} content={'# Title\n\nrewritten body\n'} />)
    }).not.toThrow()
    expect(await screen.findByText(/rewritten body/)).toBeInTheDocument()
  })
})
