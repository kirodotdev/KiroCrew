/**
 * Tests for MarkdownPanel's reveal-a-cited-line contract.
 *
 * The trap this locks: `ContentRenderer` dispatches on `isRichType` BEFORE it
 * looks at `editing`, so forcing source mode was not enough on its own — a
 * `data.csv:42` citation set `editing = true` and still rendered the viewer, no
 * editor mounted, and the requested jump silently did not happen.
 *
 * The inverse matters too: an image or PDF has no text source, so a
 * `diagram.png:42` citation must NOT be dragged into an editor showing base64.
 *
 * Post-Pierre the reveal is TWO effects with a handle between them, so this
 * suite asserts both halves rather than only which renderer won:
 *
 *   1. `revealLine` + `revealTargetsSource` flips the panel into source mode,
 *      which is the commit that MOUNTS the editor.
 *   2. The editor's imperative handle arrives as STATE (`setRevealEditor` is
 *      the callback ref), so the reveal effect re-runs on that commit and calls
 *      `jumpToLine`. A ref would attach without a render and strand the jump —
 *      which is exactly the ordering every markdown citation takes, since the
 *      request always predates the editor.
 *
 * Pierre is stubbed (heavy, lazy, paints into a shadow root that never resolves
 * under vitest) following the `EditableCodeBlock.cacheKey` precedent. The stub
 * implements the handle, so `jumpToLine` is observable — that is the panel's
 * side of the contract and the only part it owns.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { forwardRef, useImperativeHandle } from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PierreEditorHandle } from '../pierre'

const hoisted = vi.hoisted(() => ({ jumpToLine: vi.fn(), focus: vi.fn() }))

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreEditor: forwardRef<PierreEditorHandle, { file: { contents: string } }>(
    function PierreEditorStub({ file }, ref) {
      useImperativeHandle(ref, () => ({ jumpToLine: hoisted.jumpToLine, focus: hoisted.focus }), [])
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
    artifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
    artifact: vi.fn().mockResolvedValue({}),
    fileDiff: vi.fn().mockResolvedValue({ diff: '', original: '', status: 'clean' }),
    revealPath: vi.fn(),
  },
}))

const { default: MarkdownPanel } = await import('../components/MarkdownPanel')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter><QueryClientProvider client={qc}>{children}</QueryClientProvider></MemoryRouter>
)

beforeEach(() => {
  qc.clear()
  hoisted.jumpToLine.mockClear()
  hoisted.focus.mockClear()
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, headers: { get: () => null }, json: async () => ({}), text: async () => '',
  })))
})

function panelProps(filePath: string, content: string, revealLine?: { line: number; nonce: number }, onRevealConsumed?: () => void) {
  return {
    embedded: true as const,
    filePath,
    content,
    onContentChange: () => {},
    onSave: async () => {},
    onClose: () => {},
    revealLine,
    onRevealConsumed,
  }
}

function mount(filePath: string, content: string, revealLine?: { line: number; nonce: number }, onRevealConsumed?: () => void) {
  return render(<MarkdownPanel {...panelProps(filePath, content, revealLine, onRevealConsumed)} />, { wrapper })
}

describe('MarkdownPanel — a cited line forces the renderer that has lines', () => {
  it('forces SOURCE mode for a cited markdown file, not the rendered preview', async () => {
    // Markdown opens in preview by default, and preview has no per-line element to
    // scroll to (`data-sourcepos` is per block, soft wrap breaks any line
    // correspondence), so a citation has to switch it to source. This is the case
    // the reveal genuinely changes.
    mount('/x/notes.md', '# a\n\nb\n\nc\n', { line: 3, nonce: 1 })
    expect(await screen.findByTestId('pierre-editor')).toBeInTheDocument()
  })

  it('jumps the mounted editor to the cited line through its imperative handle', async () => {
    // The whole point of forcing source mode. The handle only exists on the
    // commit AFTER the flip, so this also proves the state-backed handle
    // re-runs the reveal effect.
    mount('/x/notes.md', '# a\n\nb\n\nc\n', { line: 3, nonce: 1 })
    await screen.findByTestId('pierre-editor')
    await waitFor(() => expect(hoisted.jumpToLine).toHaveBeenCalledWith(3, undefined))
  })

  it('forwards a cited line range, not just its start', async () => {
    // `path:2410-2465` carries an endLine the whole way from the chip; dropping
    // it here highlighted only the first row of the span.
    mount('/x/notes.md', '# a\n\nb\n\nc\n\nd\n', { line: 3, endLine: 5, nonce: 1 })
    await screen.findByTestId('pierre-editor')
    await waitFor(() => expect(hoisted.jumpToLine).toHaveBeenCalledWith(3, 5))
  })

  it('tells the owner the reveal landed so the one-shot target can be dropped', async () => {
    const onRevealConsumed = vi.fn()
    mount('/x/notes.md', '# a\n\nb\n', { line: 2, nonce: 1 }, onRevealConsumed)
    await waitFor(() => expect(onRevealConsumed).toHaveBeenCalledTimes(1))
  })

  it('leaves a markdown file in preview when no line was cited', async () => {
    mount('/x/notes.md', '# a\n\nb\n')
    await waitFor(() => expect(screen.queryByTestId('pierre-editor')).toBeNull())
    expect(hoisted.jumpToLine).not.toHaveBeenCalled()
  })

  it('opens a cited rich file in its own viewer and drops the line', async () => {
    // A deliberate scope line, not an oversight. `isRichType` gates the
    // source/preview toggle, the line-number and diff controls and Cmd+S, so
    // forcing a rich file into an editor produced a file in source mode with no
    // way back to its viewer and no visible Save for a buffer the user had
    // edited. Opening the viewer without jumping still beats the inert chip this
    // replaced, and it strands nothing.
    mount('/x/data.csv', 'a,b\n1,2\n', { line: 2, nonce: 1 })
    await waitFor(() => expect(screen.queryByTestId('pierre-editor')).toBeNull())
    expect(hoisted.jumpToLine).not.toHaveBeenCalled()
    // And no half-built source-mode chrome is exposed for it.
    expect(screen.queryByText(/view preview/i)).toBeNull()
    expect(screen.queryByText(/view source/i)).toBeNull()
  })

  it('does NOT drag an image into an editor for a cited line', async () => {
    // `content` for an image is base64; the editor would show that instead of
    // the picture. Asserted POSITIVELY (the image renders) — "no editor" alone
    // would also pass if the file simply failed to render at all.
    mount('/x/diagram.png', 'iVBORw0KGgo=', { line: 42, nonce: 1 })
    await waitFor(() => expect(document.querySelector('img')).not.toBeNull())
    expect(screen.queryByTestId('pierre-editor')).toBeNull()
  })

  it('mounts the editor for an ordinary code file citation', async () => {
    mount('/x/mod.py', 'a = 1\nb = 2\n', { line: 2, nonce: 1 })
    expect(await screen.findByTestId('pierre-editor')).toBeInTheDocument()
    await waitFor(() => expect(hoisted.jumpToLine).toHaveBeenCalledWith(2, undefined))
  })
})

/**
 * The nonce is the whole reason `revealLine` is an object: clicking the same
 * `notes.md:3` chip again, after scrolling away, must re-fire, and a bare
 * `line: 3` prop is `===` to the previous one so no effect would run.
 */
describe('MarkdownPanel — the reveal nonce is what makes a repeat click fire', () => {
  it('reveals once per nonce, even as the panel re-renders', async () => {
    const { rerender } = mount('/x/notes.md', 'a\nb\nc\n', { line: 2, nonce: 7 })
    await waitFor(() => expect(hoisted.jumpToLine).toHaveBeenCalledTimes(1))
    // Same target object shape, same nonce: an idempotent re-render must not
    // yank the reader back to the line they scrolled away from.
    rerender(<MarkdownPanel {...panelProps('/x/notes.md', 'a\nb\nc\n', { line: 2, nonce: 7 })} />)
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(hoisted.jumpToLine).toHaveBeenCalledTimes(1)
  })

  it('re-fires for the same line under a new nonce', async () => {
    const { rerender } = mount('/x/notes.md', 'a\nb\nc\n', { line: 2, nonce: 7 })
    await waitFor(() => expect(hoisted.jumpToLine).toHaveBeenCalledTimes(1))
    rerender(<MarkdownPanel {...panelProps('/x/notes.md', 'a\nb\nc\n', { line: 2, nonce: 8 })} />)
    await waitFor(() => expect(hoisted.jumpToLine).toHaveBeenCalledTimes(2))
    expect(hoisted.jumpToLine).toHaveBeenLastCalledWith(2, undefined)
  })

  it('pulls the panel back out of preview when a second chip click arrives', async () => {
    // The view-forcing effect is keyed on the whole target, nonce included, so
    // a reader who switched back to preview in between is returned to source.
    const { rerender } = mount('/x/notes.md', 'a\nb\nc\n', { line: 2, nonce: 1 })
    await screen.findByTestId('pierre-editor')
    // Back to the rendered preview by hand.
    screen.getByText('Preview').click()
    await waitFor(() => expect(screen.queryByTestId('pierre-editor')).toBeNull())
    rerender(<MarkdownPanel {...panelProps('/x/notes.md', 'a\nb\nc\n', { line: 3, nonce: 2 })} />)
    expect(await screen.findByTestId('pierre-editor')).toBeInTheDocument()
    await waitFor(() => expect(hoisted.jumpToLine).toHaveBeenLastCalledWith(3, undefined))
  })
})
