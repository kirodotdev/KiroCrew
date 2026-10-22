/**
 * End to end through the file viewer: drag a box on a rendered image, write the
 * comment, and submit the batch. Pins what the panel owns: the pending row's
 * label, the box drawn back on the image, the row a click on the box flashes,
 * and the chat payload line.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createRef, forwardRef, useImperativeHandle } from 'react'
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react'
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

// The CSS Custom Highlight API, which happy-dom lacks: MarkdownPanel reads it at
// load, and without it the text-highlight hover a box's popover shares the
// scroll area with never runs.
class StubHighlight { constructor(..._ranges: Range[]) {} }
vi.stubGlobal('Highlight', StubHighlight)
vi.stubGlobal('CSS', { highlights: { set: () => {}, delete: () => true } })

const { api } = await import('../api/client')
const { default: MarkdownPanel } = await import('../components/MarkdownPanel')
const { __resetComposerDraftsForTests } = await import('../utils/composerDraftStore')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter>
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  </MemoryRouter>
)

const box = { offsetLeft: 0, offsetTop: 0, offsetWidth: 400, offsetHeight: 200 }
const saved = new Map<string, PropertyDescriptor | undefined>()
const RECT = { left: 100, top: 50, width: 400, height: 200, right: 500, bottom: 250, x: 100, y: 50, toJSON: () => ({}) } as DOMRect

beforeEach(() => {
  vi.clearAllMocks()
  qc.clear()
  localStorage.clear()
  sessionStorage.clear()
  __resetComposerDraftsForTests()
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
  for (const [k, v] of Object.entries(box)) {
    saved.set(k, Object.getOwnPropertyDescriptor(HTMLElement.prototype, k))
    Object.defineProperty(HTMLElement.prototype, k, { configurable: true, get: () => v })
  }
})

afterEach(() => {
  for (const [k, d] of saved) if (d) Object.defineProperty(HTMLElement.prototype, k, d)
  vi.restoreAllMocks()
})

const DOC = '# States\n\nThe configured state:\n\n![Fully configured](states/fully-configured.png)\n'

const mount = (doc = DOC) => {
  const onSubmitComments = vi.fn()
  render(
    <MarkdownPanel
      embedded
      filePath="/tmp/doc.md"
      content={doc}
      onContentChange={vi.fn()}
      onSave={vi.fn(async () => {})}
      onClose={vi.fn()}
      onSubmitComments={onSubmitComments}
      initialDiffMode={false}
    />,
    { wrapper },
  )
  return onSubmitComments
}

/** Load the image and drag from 25%, 25% to 75%, 75% of it; returns the box's input. */
const drawBox = async (alt: string) => {
  const img = await screen.findByRole('img', { name: alt })
  fireEvent.load(img)
  img.getBoundingClientRect = () => RECT
  await waitFor(() => expect(document.querySelector('[data-image-comment-layer]')).not.toBeNull())
  fireEvent.pointerDown(img, { clientX: 200, clientY: 100, pointerId: 1, button: 0 })
  fireEvent.pointerMove(window, { clientX: 400, clientY: 200, pointerId: 1 })
  fireEvent.pointerUp(window, { clientX: 400, clientY: 200, pointerId: 1 })
  fireEvent.click(img)
  return screen.findByLabelText('Comment on the boxed area')
}

/** Load the image, drag from 25%, 25% to 75%, 75% of it, and comment `text`. */
const boxAndComment = async (alt: string, text: string) => {
  const img = await screen.findByRole('img', { name: alt })
  fireEvent.load(img)
  img.getBoundingClientRect = () => RECT
  await waitFor(() => expect(document.querySelector('[data-image-comment-layer]')).not.toBeNull())
  // A drag on the image draws a box, so the browser must not carry the image out.
  expect(img).toHaveAttribute('draggable', 'false')
  fireEvent.pointerDown(img, { clientX: 200, clientY: 100, pointerId: 1, button: 0 })
  fireEvent.pointerMove(window, { clientX: 400, clientY: 200, pointerId: 1 })
  fireEvent.pointerUp(window, { clientX: 400, clientY: 200, pointerId: 1 })
  fireEvent.click(img)
  const input = await screen.findByLabelText('Comment on the boxed area')
  // The same box a text selection opens, with no Copy: nothing is selected.
  expect(input.closest('[data-image-comment-composer]')).not.toBeNull()
  expect(screen.queryByRole('button', { name: /^Copy/ })).toBeNull()
  fireEvent.change(input, { target: { value: text } })
  fireEvent.keyDown(input, { key: 'Enter' })
  return img
}

describe('MarkdownPanel image box comments', () => {
  /** The panel on `doc`, with a way to swap the document under it, as an edit
   *  from the editor or the agent does while comments are pending. */
  const mountEditable = (doc: string) => {
    const props = {
      embedded: true,
      filePath: '/tmp/doc.md',
      onContentChange: vi.fn(),
      onSave: vi.fn(async () => {}),
      onClose: vi.fn(),
      onSubmitComments: vi.fn(),
      initialDiffMode: false,
    }
    const { rerender } = render(<MarkdownPanel {...props} content={doc} />, { wrapper })
    return (next: string) => rerender(<MarkdownPanel {...props} content={next} />)
  }
  /** The alt of the image a box button is drawn on. */
  const imageUnder = (button: HTMLElement) =>
    button.closest('[data-image-comment-layer]')?.parentElement?.querySelector('img')?.getAttribute('alt')
  /** Load every image, as the browser does again for a re-rendered one. */
  const loadImages = async () => { for (const img of await screen.findAllByRole('img')) fireEvent.load(img) }

  it('keeps a saved box on its image after an edit above moves the image to another line', async () => {
    const setDoc = mountEditable(DOC)
    await boxAndComment('Fully configured', 'badge overlaps the title')
    expect(await screen.findByRole('button', { name: 'Box 1 at 25%, 25%' })).toBeInTheDocument()

    setDoc('# States\n\nA new paragraph.\n\nThe configured state:\n\n![Fully configured](states/fully-configured.png)\n')
    await screen.findByText('A new paragraph.')
    await loadImages()
    const boxButton = await screen.findByRole('button', { name: 'Box 1 at 25%, 25%' })
    expect(imageUnder(boxButton)).toBe('Fully configured')
  })

  it('keeps a box on the same use of an image the document shows twice, after an edit above both', async () => {
    const twice = '# States\n\n![First use](states/a.png)\n\nBetween.\n\n![Second use](states/a.png)\n'
    const setDoc = mountEditable(twice)
    // Only the second use has loaded when the box is drawn, as with a lazy image
    // above the fold, and it still counts as the 2nd of 2.
    await boxAndComment('Second use', 'here')
    expect(imageUnder(await screen.findByRole('button', { name: 'Box 1 at 25%, 25%' }))).toBe('Second use')

    // Four lines above both uses: the first use lands on the line the box was
    // drawn at, so only knowing which use it was keeps the box on the second.
    setDoc(`Intro.\n\nMore intro.\n\n${twice}`)
    await screen.findByText('More intro.')
    await loadImages()
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Box 1 at 25%, 25%' })).toHaveLength(1))
    expect(imageUnder(screen.getByRole('button', { name: 'Box 1 at 25%, 25%' }))).toBe('Second use')
  })
  it('adds a box comment that lists, draws back on the image and submits with its box', async () => {
    const onSubmitComments = mount()
    await boxAndComment('Fully configured', 'badge overlaps the title')

    expect(await screen.findByText('Box 1 on "Fully configured"')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Box 1 at 25%, 25%' })).toHaveStyle({ left: '25%', top: '25%', width: '50%', height: '50%' })

    fireEvent.click(screen.getByRole('button', { name: /Submit All/ }))
    await waitFor(() => expect(onSubmitComments).toHaveBeenCalledTimes(1))
    // Named by the file the preview loaded, which the backend attaches.
    expect((onSubmitComments.mock.calls[0][0] as string).split('\n'))
      .toContain('1. (line 5, image "/tmp/states/fully-configured.png", box x=0.250, y=0.250, w=0.500, h=0.500): "badge overlaps the title"')
    await waitFor(() => expect(screen.queryByText('Box 1 on "Fully configured"')).toBeNull())
  })

  it('shows a box\'s comment in the popover a hover on highlighted text shows', async () => {
    mount()
    await boxAndComment('Fully configured', 'badge overlaps the title')
    const boxButton = await screen.findByRole('button', { name: 'Box 1 at 25%, 25%' })
    boxButton.getBoundingClientRect = () => ({ left: 200, top: 100, width: 200, height: 100, right: 400, bottom: 200, x: 200, y: 100, toJSON: () => ({}) })
    fireEvent.pointerMove(boxButton, { clientX: 300, clientY: 150, pointerId: 1 })
    const tip = document.querySelector('.mc-comment-tooltip') as HTMLElement
    expect(tip).not.toBeNull()
    expect(tip).toHaveTextContent('badge overlaps the title')
    // Beside the box, not over the region the comment is about.
    expect(tip.style.left).toBe('408px')
    expect(tip.style.top).toBe('100px')
    // The text-highlight hover, which runs on the same scroll area, leaves it up.
    fireEvent.mouseMove(boxButton, { clientX: 301, clientY: 151 })
    expect(document.querySelector('.mc-comment-tooltip')).not.toBeNull()
    fireEvent.pointerLeave(boxButton, { clientX: 520, clientY: 150, pointerId: 1 })
    expect(document.querySelector('.mc-comment-tooltip')).toBeNull()
  })

  it('flashes the comment\'s pending row when its box is clicked, as highlighted text does', async () => {
    mount()
    await boxAndComment('Fully configured', 'here')
    const row = (await screen.findByText('Box 1 on "Fully configured"')).closest('[data-comment-id]') as HTMLElement
    const scroll = vi.fn()
    row.scrollIntoView = scroll
    const boxButton = screen.getByRole('button', { name: 'Box 1 at 25%, 25%' })
    fireEvent.pointerDown(boxButton, { clientX: 300, clientY: 150, pointerId: 1, button: 0 })
    fireEvent.pointerUp(window, { clientX: 300, clientY: 150, pointerId: 1 })
    fireEvent.click(boxButton)
    expect(scroll).toHaveBeenCalled()
  })

  it('sends the line the box was drawn on after an edit above, as a text comment sends its own', async () => {
    const onSubmitComments = vi.fn()
    const props = {
      embedded: true,
      filePath: '/tmp/doc.md',
      onContentChange: vi.fn(),
      onSave: vi.fn(async () => {}),
      onClose: vi.fn(),
      onSubmitComments,
      initialDiffMode: false,
    }
    const { rerender } = render(<MarkdownPanel {...props} content={DOC} />, { wrapper })
    await boxAndComment('Fully configured', 'here')
    rerender(<MarkdownPanel {...props} content={'# States\n\nA new paragraph.\n\nThe configured state:\n\n![Fully configured](states/fully-configured.png)\n'} />)
    await screen.findByText('A new paragraph.')
    await loadImages()
    await screen.findByRole('button', { name: 'Box 1 at 25%, 25%' })
    fireEvent.click(screen.getByRole('button', { name: /Submit All/ }))
    await waitFor(() => expect(onSubmitComments).toHaveBeenCalledTimes(1))
    expect((onSubmitComments.mock.calls[0][0] as string).split('\n'))
      .toContain('1. (line 5, image "/tmp/states/fully-configured.png", box x=0.250, y=0.250, w=0.500, h=0.500): "here"')
  })

  // The viewer renders the text after a code fence as its own block, parsed on
  // its own, so an image's parsed line counts from the start of that block.
  it('sends the document line of an image below a code fence, not its line within the block', async () => {
    const onSubmitComments = mount('# T\n\n```\na\n```\n\n![After fence](states/b.png)\n')
    // drawBox, not boxAndComment: the code block brings its own Copy button.
    const input = await drawBox('After fence')
    fireEvent.change(input, { target: { value: 'here' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.click(await screen.findByRole('button', { name: /Submit All/ }))
    await waitFor(() => expect(onSubmitComments).toHaveBeenCalledTimes(1))
    expect((onSubmitComments.mock.calls[0][0] as string).split('\n'))
      .toContain('1. (line 7, image "/tmp/states/b.png", box x=0.250, y=0.250, w=0.500, h=0.500): "here"')
  })

  it('keeps a box on its own use of an image shown above and below a code fence', async () => {
    mount('![First use](states/a.png)\n\n```\nx\n```\n![Second use](states/a.png)\n')
    const input = await drawBox('Second use')
    fireEvent.change(input, { target: { value: 'here' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await loadImages()
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Box 1 at 25%, 25%' })).toHaveLength(1))
    expect(imageUnder(screen.getByRole('button', { name: 'Box 1 at 25%, 25%' }))).toBe('Second use')
  })

  it('names a box on an image with no alt text by its file name', async () => {
    mount('# States\n\n![](states/fully-configured.png)\n')
    await waitFor(() => expect(document.querySelector('img')).not.toBeNull())
    const img = document.querySelector('img') as HTMLImageElement
    fireEvent.load(img)
    img.getBoundingClientRect = () => RECT
    await waitFor(() => expect(document.querySelector('[data-image-comment-layer]')).not.toBeNull())
    fireEvent.pointerDown(img, { clientX: 200, clientY: 100, pointerId: 1, button: 0 })
    fireEvent.pointerMove(window, { clientX: 400, clientY: 200, pointerId: 1 })
    fireEvent.pointerUp(window, { clientX: 400, clientY: 200, pointerId: 1 })
    fireEvent.click(img)
    const input = await screen.findByLabelText('Comment on the boxed area')
    fireEvent.change(input, { target: { value: 'no alt here' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(await screen.findByText('Box 1 on "fully-configured.png"')).toBeInTheDocument()
  })

  it('keeps a box comment pending when the send is refused', async () => {
    const onSubmitComments = mount()
    onSubmitComments.mockReturnValue(false)
    await boxAndComment('Fully configured', 'here')
    fireEvent.click(screen.getByRole('button', { name: /Submit All/ }))
    await waitFor(() => expect(onSubmitComments).toHaveBeenCalledTimes(1))
    await new Promise(r => setTimeout(r, 0))
    expect(screen.getByText('Box 1 on "Fully configured"')).toBeInTheDocument()
    // Offered again: the refused send released the in-flight guard.
    fireEvent.click(screen.getByRole('button', { name: /Submit All/ }))
    await waitFor(() => expect(onSubmitComments).toHaveBeenCalledTimes(2))
  })

  it('names a wrapped destination with a space by its decoded path, not the encoded one', async () => {
    const onSubmitComments = mount('# Doc\n\n![Wrapped](<shots/a b.png>)\n')
    await boxAndComment('Wrapped', 'here')
    const row = await screen.findByText(/^Box 1 on "Wrapped"/)
    // The pending row keeps the author's destination, as the renderer got it.
    expect(row.getAttribute('title')).toBe('shots/a%20b.png')
    fireEvent.click(screen.getByRole('button', { name: /Submit All/ }))
    await waitFor(() => expect(onSubmitComments).toHaveBeenCalledTimes(1))
    expect(onSubmitComments.mock.calls[0][0]).toContain('image "/tmp/shots/a b.png", box')
  })

})

describe('MarkdownPanel image box comment drafts, guarded like a text draft', () => {
  const composer = () => document.querySelector('[data-image-comment-composer]') as HTMLElement | null
  const panelProps = {
    embedded: true,
    filePath: '/tmp/doc.md',
    content: DOC,
    onContentChange: vi.fn(),
    onSave: vi.fn(async () => {}),
    onClose: vi.fn(),
    onSubmitComments: vi.fn(),
    initialDiffMode: false,
  }

  it('keeps a typed comment open on a click outside, and closes an empty one', async () => {
    mount()
    const input = await drawBox('Fully configured')
    fireEvent.change(input, { target: { value: 'not yet added' } })
    fireEvent.mouseDown(document.body)
    expect(screen.getByLabelText('Comment on the boxed area')).toHaveValue('not yet added')

    fireEvent.change(screen.getByLabelText('Comment on the boxed area'), { target: { value: '' } })
    fireEvent.mouseDown(document.body)
    await waitFor(() => expect(composer()).toBeNull())
  })

  it('stays inside the panel\'s column when it fits there, as the text composer does', async () => {
    mount()
    const img = await screen.findByRole('img', { name: 'Fully configured' })
    // The panel's scroll area: a column from 300 to 900, so a box centred
    // under the drag's end (left 200) would spill over whatever sits beside it.
    const scroller = img.closest('.overflow-auto') as HTMLElement
    scroller.getBoundingClientRect = () => ({ left: 300, top: 0, width: 600, height: 700, right: 900, bottom: 700, x: 300, y: 0, toJSON: () => ({}) })
    await drawBox('Fully configured')
    await waitFor(() => expect(composer()!.style.visibility).not.toBe('hidden'))
    expect(parseFloat(composer()!.style.left)).toBe(308)
  })

  it('is clamped to the viewport again when the viewport is resized', async () => {
    mount()
    const input = await drawBox('Fully configured')
    fireEvent.change(input, { target: { value: 'typed' } })
    await waitFor(() => expect(composer()!.style.visibility).not.toBe('hidden'))
    // 400 wide, hung off the drag's end at x=400: centred at left 200 while
    // the viewport is wide, and pulled in to 500 - 400 - 8 when it narrows.
    expect(parseFloat(composer()!.style.left)).toBe(200)
    const width = window.innerWidth
    try {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: 500 })
      fireEvent(window, new Event('resize'))
      await waitFor(() => expect(parseFloat(composer()!.style.left)).toBe(92))
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
    }
    expect(screen.getByLabelText('Comment on the boxed area')).toHaveValue('typed')
  })

  it('moves with a scroll of the panel instead of closing', async () => {
    mount()
    const input = await drawBox('Fully configured')
    fireEvent.change(input, { target: { value: 'typed' } })
    await waitFor(() => expect(composer()!.style.visibility).not.toBe('hidden'))
    const top = parseFloat(composer()!.style.top)
    const scroller = screen.getByRole('img', { name: 'Fully configured' }).closest('.min-h-0.overflow-auto') as HTMLElement
    Object.defineProperty(scroller, 'scrollTop', { configurable: true, value: 50 })
    fireEvent.scroll(scroller)
    await waitFor(() => expect(parseFloat(composer()!.style.top)).toBe(top - 50))
    expect(screen.getByLabelText('Comment on the boxed area')).toHaveValue('typed')
  })

  it('asks before Escape discards typed text: Cancel keeps it, Discard closes with nothing added', async () => {
    mount()
    const input = await drawBox('Fully configured')
    fireEvent.change(input, { target: { value: 'typed' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Discard your unsaved comment?')).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByLabelText('Comment on the boxed area')).toHaveValue('typed')

    fireEvent.keyDown(screen.getByLabelText('Comment on the boxed area'), { key: 'Escape' })
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Discard comment' }))
    await waitFor(() => expect(composer()).toBeNull())
    expect(screen.queryByText(/Box \d+ on/)).toBeNull()
  })

  it('closes an empty box on Escape without asking', async () => {
    mount()
    fireEvent.keyDown(await drawBox('Fully configured'), { key: 'Escape' })
    await waitFor(() => expect(composer()).toBeNull())
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('asks before a full-screen Escape outside the box discards typed text', async () => {
    mount()
    fireEvent.click(screen.getByTestId('markdown-panel-more-options'))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Full screen' }))
    await screen.findByRole('button', { name: 'Exit full screen' })
    fireEvent.change(await drawBox('Fully configured'), { target: { value: 'typed' } })
    // Focus leaves the box, so the Escape reaches the panel, not the box.
    fireEvent.mouseDown(document.body)
    fireEvent.keyDown(document, { key: 'Escape' })
    // The full-screen view is a dialog too, so the prompt is found by its text.
    const prompt = (await screen.findByText('Discard your unsaved comment?')).closest('[role="dialog"]') as HTMLElement
    fireEvent.click(within(prompt).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByText('Discard your unsaved comment?')).toBeNull())
    expect(screen.getByRole('button', { name: 'Exit full screen' })).toBeInTheDocument()
    expect(screen.getByLabelText('Comment on the boxed area')).toHaveValue('typed')
  })

  it('asks before Edit discards a typed box comment', async () => {
    mount()
    fireEvent.change(await drawBox('Fully configured'), { target: { value: 'typed' } })
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Discard your unsaved comment?')).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByLabelText('Comment on the boxed area')).toHaveValue('typed')
  })

  it('brings the typed text back when the tab returns from the background', async () => {
    const { rerender } = render(<MarkdownPanel {...panelProps} active />, { wrapper })
    fireEvent.change(await drawBox('Fully configured'), { target: { value: 'still here' } })
    rerender(<MarkdownPanel {...panelProps} active={false} />)
    await waitFor(() => expect(composer()).toBeNull())
    rerender(<MarkdownPanel {...panelProps} active />)
    expect(await screen.findByLabelText('Comment on the boxed area')).toHaveValue('still here')
  })

  it('opens below its box again after a scroll, then a tab switch away and back', async () => {
    const { rerender } = render(<MarkdownPanel {...panelProps} active />, { wrapper })
    const input = await drawBox('Fully configured')
    fireEvent.change(input, { target: { value: 'typed' } })
    await waitFor(() => expect(composer()!.style.visibility).not.toBe('hidden'))
    const top = parseFloat(composer()!.style.top)
    // The panel scrolls 100px, taking the image up with it.
    const img = screen.getByRole('img', { name: 'Fully configured' })
    img.getBoundingClientRect = () => ({ ...RECT, top: RECT.top - 100, bottom: RECT.bottom - 100, y: RECT.y - 100 })
    const scroller = img.closest('.min-h-0.overflow-auto') as HTMLElement
    Object.defineProperty(scroller, 'scrollTop', { configurable: true, value: 100 })
    fireEvent.scroll(scroller)
    await waitFor(() => expect(parseFloat(composer()!.style.top)).toBe(top - 100))
    // The panel keeps its scroll while another tab is in front.
    rerender(<MarkdownPanel {...panelProps} active={false} />)
    await waitFor(() => expect(composer()).toBeNull())
    rerender(<MarkdownPanel {...panelProps} active />)
    expect(await screen.findByLabelText('Comment on the boxed area')).toHaveValue('typed')
    await waitFor(() => expect(parseFloat(composer()!.style.top)).toBe(top - 100))
  })

  it('keeps a typed comment on its box when another box is drawn', async () => {
    mount()
    const typed = await drawBox('Fully configured')
    fireEvent.change(typed, { target: { value: 'first box' } })
    // The reader has moved on, say to scroll the doc, before trying another box.
    act(() => typed.blur())
    const img = screen.getByRole('img', { name: 'Fully configured' })
    fireEvent.pointerDown(img, { clientX: 120, clientY: 60, pointerId: 2, button: 0 })
    fireEvent.pointerMove(window, { clientX: 300, clientY: 150, pointerId: 2 })
    fireEvent.pointerUp(window, { clientX: 300, clientY: 150, pointerId: 2 })
    expect(screen.getByLabelText('Comment on the boxed area')).toHaveValue('first box')
    // The drag drew nothing, so the box that kept it is brought back to the reader.
    await waitFor(() => expect(screen.getByLabelText('Comment on the boxed area')).toHaveFocus())
    fireEvent.keyDown(screen.getByLabelText('Comment on the boxed area'), { key: 'Enter' })
    expect(await screen.findByText('Box 1 on "Fully configured"')).toBeInTheDocument()
  })

  it('brings the box and its typed text back after the panel is torn down, as a slot switch does', async () => {
    const { unmount } = render(<MarkdownPanel {...panelProps} active />, { wrapper })
    fireEvent.change(await drawBox('Fully configured'), { target: { value: 'not lost to a slot switch' } })
    unmount()
    render(<MarkdownPanel {...panelProps} active />, { wrapper })
    const img = await screen.findByRole('img', { name: 'Fully configured' })
    img.getBoundingClientRect = () => RECT
    fireEvent.load(img)
    const input = await screen.findByLabelText('Comment on the boxed area')
    expect(input).toHaveValue('not lost to a slot switch')
    // The box comes back on its image, outlined while its composer is open.
    expect(document.querySelector('[data-image-box-draft]')).not.toBeNull()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(await screen.findByText('Box 1 on "Fully configured"')).toBeInTheDocument()
    // Once added it is an ordinary pending comment, so nothing is kept for the box.
    expect(sessionStorage.getItem('mc-image-box-draft:/tmp/doc.md')).toBeNull()
  })

  it('brings a torn-down box back to its image after an edit above moved the image', async () => {
    const { unmount } = render(<MarkdownPanel {...panelProps} active />, { wrapper })
    fireEvent.change(await drawBox('Fully configured'), { target: { value: 'kept through an edit' } })
    unmount()
    // An agent edit while the panel was gone pushed the image two lines down.
    render(<MarkdownPanel {...panelProps} content={`Intro\n\n${DOC}`} active />, { wrapper })
    const img = await screen.findByRole('img', { name: 'Fully configured' })
    img.getBoundingClientRect = () => RECT
    fireEvent.load(img)
    const input = await screen.findByLabelText('Comment on the boxed area')
    expect(input).toHaveValue('kept through an edit')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(await screen.findByText('Box 1 on "Fully configured"')).toBeInTheDocument()
  })

  it('leaves a typed box to its own tab when a read-only diff tab opens the same file', async () => {
    const { unmount } = render(<MarkdownPanel {...panelProps} active />, { wrapper })
    fireEvent.change(await drawBox('Fully configured'), { target: { value: 'kept for the file tab' } })
    unmount()
    const kept = sessionStorage.getItem('mc-image-box-draft:/tmp/doc.md')
    expect(kept).not.toBeNull()
    // A read-only diff tab never shows a composer, so it neither picks the box
    // up nor asks to discard it on close, and the file tab's box stays saved.
    const onClose = vi.fn()
    const ref = createRef<MarkdownPanelHandle>()
    render(<MarkdownPanel {...panelProps} ref={ref} readOnly onClose={onClose} active />, { wrapper })
    await waitFor(() => expect(ref.current).not.toBeNull())
    act(() => ref.current!.requestClose())
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(sessionStorage.getItem('mc-image-box-draft:/tmp/doc.md')).toBe(kept)
  })

  it('hides a typed box comment while the diff view is shown, and brings it back after', async () => {
    mount()
    fireEvent.change(await drawBox('Fully configured'), { target: { value: 'not over the diff' } })
    fireEvent.click(screen.getAllByRole('button', { name: 'Toggle diff view' })[0])
    await waitFor(() => expect(composer()).toBeNull())
    fireEvent.click(screen.getAllByRole('button', { name: 'Toggle diff view' })[0])
    const img = await screen.findByRole('img', { name: 'Fully configured' })
    // The image rerenders 200px to the right, and the composer opens below the
    // box there (its middle at 500), not where it was before the diff view.
    img.getBoundingClientRect = () => ({ ...RECT, left: 300, right: 700, x: 300 })
    fireEvent.load(img)
    const input = await screen.findByLabelText('Comment on the boxed area')
    expect(input).toHaveValue('not over the diff')
    await waitFor(() => expect(composer()!.style.left).toBe('300px'))
  })

  it('keeps nothing for a box with no text, or after its text is discarded', async () => {
    const { unmount } = render(<MarkdownPanel {...panelProps} active />, { wrapper })
    const input = await drawBox('Fully configured')
    fireEvent.change(input, { target: { value: 'typed, then' } })
    fireEvent.change(input, { target: { value: '   ' } })
    unmount()
    render(<MarkdownPanel {...panelProps} active />, { wrapper })
    const img = await screen.findByRole('img', { name: 'Fully configured' })
    img.getBoundingClientRect = () => RECT
    fireEvent.load(img)
    await waitFor(() => expect(document.querySelector('[data-image-comment-layer]')).not.toBeNull())
    expect(composer()).toBeNull()
    expect(document.querySelector('[data-image-box-draft]')).toBeNull()
  })
})
