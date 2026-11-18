/**
 * A comment on text inside the frontmatter strip anchors like any other comment.
 *
 * The strip sits inside the preview root, so `anchorFromRange` gives a selection
 * there a `startOffset` and the highlighter paints the strip's own text, not a
 * passage in the body that happens to repeat it. Collapsing the strip hides its
 * rows without unmounting them, so a comment on a body heading that repeats a
 * frontmatter value stays on the heading.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { forwardRef, useImperativeHandle } from 'react'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PierreEditorHandle } from '../pierre'
import { installHighlightApiStub, registeredRanges } from './highlightApiStub'

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
    artifacts: vi.fn(), artifact: vi.fn(), createArtifact: vi.fn(), updateArtifact: vi.fn(),
    setArtifactPinned: vi.fn(), revealPath: vi.fn(), fileDiff: vi.fn(),
  },
}))

const { api } = await import('../api/client')
const { default: MarkdownPanel } = await import('../components/MarkdownPanel')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter><QueryClientProvider client={qc}>{children}</QueryClientProvider></MemoryRouter>
)

beforeEach(() => {
  vi.clearAllMocks()
  qc.clear()
  localStorage.clear()
  sessionStorage.clear()
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, headers: { get: () => null },
    json: async () => ({ enabled: false, supported_formats: [] }), text: async () => '',
  })))
  vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [] } as never)
  vi.mocked(api.artifact).mockResolvedValue({ live_dirty: false, pinned: false } as never)
  vi.mocked(api.fileDiff).mockResolvedValue({ diff: '', original: '', status: 'clean' } as never)
})

afterEach(() => { vi.restoreAllMocks() })

function mountPanel(content: string) {
  render(
    <MarkdownPanel embedded filePath="/tmp/notes.md" content={content} onContentChange={vi.fn()} onSave={vi.fn(async () => {})}
      onClose={vi.fn()} onSubmitComments={vi.fn()} initialDiffMode={false} />,
    { wrapper },
  )
}

function savedComments(): Record<string, unknown>[] {
  return JSON.parse(localStorage.getItem('mc-comment-drafts') || '{}')['/tmp/notes.md'] ?? []
}

function textNodeWith(root: HTMLElement, needle: string): Text {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  for (let n = walker.nextNode(); n; n = walker.nextNode()) if ((n as Text).data.includes(needle)) return n as Text
  throw new Error(`no text node holds ${needle}`)
}

function select(node: Text, needle: string) {
  const start = node.data.indexOf(needle)
  const range = document.createRange()
  range.setStart(node, start)
  range.setEnd(node, start + needle.length)
  const sel = window.getSelection()!
  sel.removeAllRanges()
  sel.addRange(range)
  vi.spyOn(sel, 'toString').mockReturnValue(needle)
  fireEvent.mouseUp(document)
}

async function addComment(text: string) {
  fireEvent.change(await screen.findByLabelText('Comment on the selected text'), { target: { value: text } })
  fireEvent.click(screen.getByLabelText('Add comment'))
  await screen.findByText(text)
}

/** The element each painted comment range starts in, as a tag name, so a test can say WHERE it painted. */
async function paintedIn(): Promise<string[]> {
  await waitFor(() => expect(registeredRanges('mc-comment').length).toBeGreaterThan(0))
  return registeredRanges('mc-comment').map(r => r.startContainer.parentElement?.closest('h1,p,dd')?.tagName ?? 'other')
}

const UNIQUE = '---\nname: demo\ndescription: unique frontmatter words\n---\n\n# Title\n\nbody text\n'
const REPEATED = '---\nname: demo\nowner: shared phrase here\n---\n\n# Title\n\nbody mentions shared phrase here too\n'
const HEADING = '---\ntitle: Alpha topic\n---\n\n# Alpha topic\n\nsome text\n'

describe('MarkdownPanel: a comment inside the frontmatter strip', () => {
  it('saves an offset and underlines text that appears only in the strip', async () => {
    mountPanel(UNIQUE)
    const strip = await screen.findByTestId('md-frontmatter')
    select(textNodeWith(strip, 'unique frontmatter words'), 'unique frontmatter words')
    await addComment('strip only')
    expect(savedComments()[0]).toMatchObject({ anchor: 'unique frontmatter words', line: 3 })
    expect(typeof savedComments()[0].startOffset).toBe('number')
    expect(await paintedIn()).toEqual(['DD'])
  })

  it('underlines the strip occurrence, not the body passage that repeats it', async () => {
    mountPanel(REPEATED)
    const strip = await screen.findByTestId('md-frontmatter')
    select(textNodeWith(strip, 'shared phrase here'), 'shared phrase here')
    await addComment('strip copy')
    expect(await paintedIn()).toEqual(['DD'])
  })

  it('keeps a body comment on a heading that repeats a strip value after the strip collapses', async () => {
    mountPanel(HEADING)
    const strip = await screen.findByTestId('md-frontmatter')
    const h1 = document.querySelector('.msg-content h1') as HTMLElement
    select(textNodeWith(h1, 'Alpha topic'), 'Alpha topic')
    await addComment('on the heading')
    expect(await paintedIn()).toEqual(['H1'])
    fireEvent.click(within(strip).getByRole('button'))
    await waitFor(() => expect(strip.querySelector('dl')?.hasAttribute('hidden')).toBe(true))
    expect(await paintedIn()).toEqual(['H1'])
  })

  it('saves the source line of the field the comment was made on when two fields share a value', async () => {
    mountPanel('---\nname: demo\ntitle: demo\n---\n\n# T\n\nx\n')
    const strip = await screen.findByTestId('md-frontmatter')
    const rows = strip.querySelectorAll('dd')
    select(rows[1].firstChild as Text, 'demo')
    await addComment('on the second field')
    expect(savedComments()[0]).toMatchObject({ anchor: 'demo', line: 3, column: 8 })
    expect(registeredRanges('mc-comment').map(r => r.startContainer.parentElement)).toEqual([rows[1]])
  })
})

describe('MarkdownPanel: the frontmatter strip in fullscreen', () => {
  it('renders the strip, not the raw block, once the file goes fullscreen, and collapses there', async () => {
    mountPanel(UNIQUE)
    await screen.findByTestId('md-frontmatter')
    fireEvent.click(screen.getAllByTestId('markdown-panel-more-options')[0])
    fireEvent.click(screen.getByText('Full screen'))
    await screen.findByTestId('markdown-panel-fullscreen-header')
    // The side panel body unmounts while fullscreen, so the one strip left is the overlay's.
    await waitFor(() => expect(screen.getAllByTestId('md-frontmatter')).toHaveLength(1))
    const strips = screen.getAllByTestId('md-frontmatter')
    const strip = within(strips[0])
    expect(strip.getByText('Frontmatter · 2 fields')).toBeInTheDocument()
    expect(strip.getByText('unique frontmatter words')).toBeInTheDocument()
    // No raw `key: value` line anywhere (the strip shows keys and values in separate cells).
    expect(document.body.textContent).not.toMatch(/description:\s*unique/)
    expect(document.body.textContent).not.toMatch(/name:\s*demo/)
    fireEvent.click(strip.getByRole('button', { expanded: true }))
    expect(strip.getByRole('button', { expanded: false })).toBeInTheDocument()
    expect(strips[0].querySelector('dl')).toHaveAttribute('hidden')
  })
})
