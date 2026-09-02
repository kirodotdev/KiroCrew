/**
 * Second-wave cold-path tests for `MarkdownPanel`.
 *
 * `MarkdownPanel.test.tsx` covers the ⋯ menu inventory, `MarkdownPanelCoverage`
 * covers the resolvers plus most of the panel chrome. What neither of them ever
 * enters, and what this file aims at:
 *
 *   - the ⋯ menu's own artifact / knowledge entries (the header icon buttons are
 *     a DIFFERENT component — clicking them never runs the menu's handlers),
 *   - the menu's WAI-ARIA Escape path, which returns focus to the trigger,
 *   - the artifact-detail fetch falling back when `api.artifact` rejects,
 *   - the snapshot mutation's error path,
 *   - the discard-to-disk path when the host supplies no `onRefresh`,
 *   - the diff surface's read-only/editable split: which component the panel
 *     mounts for `diffMode` alone versus `diffMode + editing`, and what it hands
 *     each of them,
 *   - the comment-highlight click → row flash, and the pointer miss,
 *   - fullscreen for a CODE file, where the overlay carries the only copy of
 *     the editor toolbar.
 *
 * `Highlight` / `CSS.highlights` are stubbed BEFORE the dynamic import because
 * MarkdownPanel captures both into module-level constants at load time.
 *
 * Pierre is stubbed rather than driven. Its predecessor's integration lived in
 * `beforeMount`/`onMount` callbacks the panel supplied, so the old mock had to
 * invoke them or leave that code unexecuted; Pierre inverts that — theme
 * registration, diff navigation and selection all live INSIDE the shadow root
 * it owns, and the panel's remaining side of the contract is which surface it
 * mounts and which props it passes. That is what the stub exposes.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { forwardRef, useImperativeHandle } from 'react'
import { render, screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
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

// ── Pierre stubs that report the props the panel hands them ──────────────────
// `data-diff-base` is the whole point of the editable-diff stub: `undefined`
// renders the plain editor, a string renders the live-diff editing surface, and
// the panel is what decides which.
vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreEditor: forwardRef<PierreEditorHandle, {
    file: { contents: string }; diffBase?: string | null; diffSplit?: boolean
    onChange: (v: string) => void
  }>(function PierreEditorStub({ file, diffBase, diffSplit, onChange }, ref) {
    useImperativeHandle(ref, () => ({ jumpToLine: () => {}, focus: () => {} }), [])
    return (
      <div
        data-testid="pierre-editor"
        data-value={file.contents}
        data-diff-base={diffBase === undefined ? 'none' : String(diffBase)}
        data-diff-split={String(!!diffSplit)}
      >
        <button
          data-testid="pierre-editor-emit"
          aria-label="emit an edit from the stubbed editor"
          onClick={() => onChange('edited inside pierre')}
        />
      </div>
    )
  }),
  PierreCode: ({ file }: { file: { contents: string } }) => (
    <div data-testid="pierre-code" data-value={file.contents} />
  ),
  PierreFilePair: ({ oldFile, newFile }: {
    oldFile: { contents: string } | null; newFile: { contents: string } | null
  }) => (
    <div data-testid="pierre-diff" data-old={oldFile?.contents ?? ''} data-new={newFile?.contents ?? ''} />
  ),
}))

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn(async () => true) }))

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
const { copyToClipboard } = await import('../utils/clipboard')
const { default: MarkdownPanel, OverflowMenu } = await import('../components/MarkdownPanel')

// ── fetch router ────────────────────────────────────────────────────────────
interface FetchOpts {
  knowledgeEnabled?: boolean
  fileReadText?: string
}
let fetchOpts: FetchOpts = {}

function installFetch() {
  vi.stubGlobal('fetch', vi.fn(async (input: unknown, init?: { method?: string }) => {
    const url = String(input)
    if (url.startsWith('/api/knowledge/config')) {
      return { ok: true, json: async () => ({ enabled: !!fetchOpts.knowledgeEnabled, supported_formats: ['.md', '.txt'] }) }
    }
    if (url.startsWith('/api/knowledge/sources')) {
      if (init?.method === 'POST') return { ok: true, status: 201, json: async () => ({ id: 7 }) }
      return { ok: true, json: async () => [] }
    }
    if (url.startsWith('/api/file-download')) return { ok: true, blob: async () => new Blob(['bytes']) }
    return {
      ok: true,
      status: 200,
      headers: { get: () => null },
      text: async () => fetchOpts.fileReadText ?? 'content from disk',
    }
  }))
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

/** Renders the current pathname so a navigate() can be asserted on. */
function LocationProbe() {
  const loc = useLocation()
  return <span data-testid="pathname">{loc.pathname}</span>
}

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter>
    <QueryClientProvider client={qc}>{children}<LocationProbe /></QueryClientProvider>
  </MemoryRouter>
)

beforeEach(() => {
  // The api mocks come from a module factory, so `restoreAllMocks` leaves their
  // call logs behind — one test's createArtifact call would be read as the next
  // test's.
  vi.clearAllMocks()
  qc.clear()
  fetchOpts = {}
  highlightRegistry.clear()
  localStorage.clear()
  document.getElementById('mc-comment-hl-style')?.remove()
  installFetch()
  // happy-dom has no scrollIntoView; the comment-row flash calls it directly.
  Object.defineProperty(Element.prototype, 'scrollIntoView', {
    configurable: true, writable: true, value: vi.fn(),
  })
  vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [] } as never)
  vi.mocked(api.artifact).mockResolvedValue({ live_dirty: false, pinned: false } as never)
  vi.mocked(api.createArtifact).mockResolvedValue({ slug: 'notes-md', version: 1 } as never)
  vi.mocked(api.updateArtifact).mockResolvedValue({} as never)
  vi.mocked(api.setArtifactPinned).mockResolvedValue({} as never)
  vi.mocked(api.revealPath).mockResolvedValue({ ok: true } as never)
  vi.mocked(api.fileDiff).mockResolvedValue({ diff: '', original: '', status: 'clean' } as never)
  vi.spyOn(window, 'alert').mockImplementation(() => {})
})

afterEach(() => {
  vi.restoreAllMocks()
  document.body.style.overflow = ''
  window.getSelection()?.removeAllRanges()
})

// ════════════════════════════════════════════════════════════════════════════
// The ⋯ menu's own library entries
// ════════════════════════════════════════════════════════════════════════════

function openOverflow(filePath = '/tmp/notes.md', content = '# hi\n') {
  render(<OverflowMenu filePath={filePath} content={content} />, { wrapper })
  fireEvent.click(screen.getByTestId('markdown-panel-more-options'))
}

describe('OverflowMenu — keyboard dismissal', () => {
  it('closes on Escape and hands focus back to the trigger', () => {
    openOverflow()
    const trigger = screen.getByTestId('markdown-panel-more-options')
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })
    expect(screen.queryByRole('menu')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('closes on an outside mousedown without moving focus', () => {
    openOverflow()
    fireEvent.mouseDown(document.body)
    expect(screen.queryByRole('menu')).toBeNull()
  })
})

describe('OverflowMenu — artifact entries', () => {
  it('opens the artifact from the menu once the file is already one', async () => {
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [{ slug: 'notes-md', name: 'notes.md' }] } as never)
    openOverflow()
    fireEvent.click(await screen.findByText('In Artifacts'))
    await waitFor(() => expect(screen.getByTestId('pathname')).toHaveTextContent('/artifacts/notes-md'))
  })

  it('promotes the file from the menu, re-reading it from disk', async () => {
    fetchOpts.fileReadText = '# the on-disk truth\n'
    openOverflow('/tmp/notes.md', '# a stale in-memory copy\n')
    fireEvent.click(await screen.findByText('Add to artifacts'))
    await waitFor(() => expect(api.createArtifact).toHaveBeenCalled())
    expect(vi.mocked(api.createArtifact).mock.calls[0][0]).toMatchObject({
      content: '# the on-disk truth\n', kind: 'markdown', source_path: '/tmp/notes.md',
    })
    expect(await screen.findByText('Added!')).toBeInTheDocument()
  })

  it('keeps the artifact usable when its detail fetch fails', async () => {
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [{ slug: 'notes-md', name: 'notes.md' }] } as never)
    vi.mocked(api.artifact).mockRejectedValue(new Error('detail unavailable'))
    openOverflow()
    // The query falls back to the list row rather than reporting no artifact.
    expect(await screen.findByText('In Artifacts')).toBeInTheDocument()
    expect(screen.queryByText('Add to artifacts')).toBeNull()
  })
})

describe('OverflowMenu — knowledge entry', () => {
  it('registers the file as a local source from the menu', async () => {
    fetchOpts.knowledgeEnabled = true
    openOverflow()
    fireEvent.click(await screen.findByText('Add to Knowledge'))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      '/api/knowledge/sources', expect.objectContaining({ method: 'POST' }),
    ))
    const post = vi.mocked(fetch).mock.calls.find(([, init]) => (init as { method?: string } | undefined)?.method === 'POST')!
    expect(JSON.parse((post[1] as { body: string }).body)).toEqual({
      name: 'notes.md', source_type: 'local_file', uri: '/tmp/notes.md',
    })
  })

  it('offers no knowledge entry for an unsupported extension', async () => {
    fetchOpts.knowledgeEnabled = true
    openOverflow('/tmp/mod.py', 'a = 1\n')
    await screen.findByText('Add to artifacts')
    expect(screen.queryByText('Add to Knowledge')).toBeNull()
  })
})

// ════════════════════════════════════════════════════════════════════════════
// The panel
// ════════════════════════════════════════════════════════════════════════════

interface MountOpts {
  filePath?: string
  content?: string
  savedBaseline?: string
  onRefresh?: (p: string) => Promise<void>
  onContentChange?: (c: string) => void
  onSubmitComments?: (m: string) => void
  initialDiffMode?: boolean
}

function panelProps(opts: MountOpts) {
  return {
    filePath: opts.filePath ?? '/tmp/notes.md',
    content: opts.content ?? '# Title\n\nalpha beta gamma\n',
    onContentChange: opts.onContentChange ?? vi.fn(),
    onSave: vi.fn(async () => {}),
    onClose: vi.fn(),
    onRefresh: opts.onRefresh,
    onSubmitComments: opts.onSubmitComments,
    savedBaseline: opts.savedBaseline,
    initialDiffMode: opts.initialDiffMode ?? false,
  }
}

function mountPanel(opts: MountOpts = {}) {
  const props = panelProps(opts)
  const utils = render(<MarkdownPanel embedded {...props} />, { wrapper })
  return { ...utils, props }
}

/** Open the panel's ⋯ menu (the header's — the first in the tree). */
function openPanelMenu() {
  fireEvent.click(screen.getAllByTestId('markdown-panel-more-options')[0])
}

describe('MarkdownPanel — snapshot failure', () => {
  it('surfaces the server message instead of reporting a silent snapshot', async () => {
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [{ slug: 'notes-md', name: 'notes.md' }] } as never)
    vi.mocked(api.updateArtifact).mockRejectedValue(new Error('artifact is read-only'))
    mountPanel()
    await screen.findByLabelText('Open as artifact')
    openPanelMenu()
    fireEvent.click(await screen.findByText('Snapshot version'))
    await waitFor(() => expect(window.alert).toHaveBeenCalledWith('artifact is read-only'))
  })
})

describe('MarkdownPanel — discard with no owner refresh', () => {
  it('re-reads the file itself when the host supplies no refresh hook', async () => {
    const onContentChange = vi.fn()
    fetchOpts.fileReadText = 'the version on disk'
    // Cancel lives in the unsaved-changes banner, which is mode-independent.
    mountPanel({ content: 'edited body', savedBaseline: 'disk body', onContentChange })
    fireEvent.click(screen.getByText('Cancel'))
    // The discard guard is the in-app dialog, never window.confirm.
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(onContentChange).toHaveBeenCalledWith('the version on disk'))
    expect(fetch).toHaveBeenCalledWith('/api/file-read?path=%2Ftmp%2Fnotes.md')
  })
})

describe('MarkdownPanel — selection copy', () => {
  it('copies the selected preview text through the clipboard helper', async () => {
    mountPanel()
    const para = await screen.findByText(/alpha beta gamma/)
    const textNode = para.firstChild as Text
    const range = document.createRange()
    range.setStart(textNode, 0)
    range.setEnd(textNode, 5)
    const sel = window.getSelection()!
    sel.removeAllRanges()
    sel.addRange(range)
    fireEvent.mouseUp(document)
    fireEvent.click(await screen.findByRole('button', { name: 'Copy' }))
    expect(copyToClipboard).toHaveBeenCalledWith('alpha')
  })
})

describe('MarkdownPanel — comment highlight pointer handling', () => {
  const FILE = '/tmp/notes.md'
  const BODY = 'alpha beta gamma\n'

  function seedDraft(anchor: string, text = 'why this?', file = FILE, id = 'c1') {
    localStorage.setItem('mc-comment-drafts', JSON.stringify({ [file]: [{ id, anchor, text }] }))
  }

  /** Point caretRangeFromPoint at the start of the painted comment range. */
  function caretInside(painted: Range) {
    Object.defineProperty(document, 'caretRangeFromPoint', {
      configurable: true,
      value: () => {
        const caret = document.createRange()
        caret.setStart(painted.startContainer, painted.startOffset)
        caret.collapse(true)
        return caret
      },
    })
  }

  async function mountWithPaintedComment() {
    seedDraft('beta', 'needs a citation')
    mountPanel({ filePath: FILE, content: BODY, onSubmitComments: vi.fn() })
    await waitFor(() => expect(highlightRegistry.get('mc-comment')?.length).toBe(1))
    return {
      painted: highlightRegistry.get('mc-comment')![0],
      // The scroll container the panel binds its listeners to, reached through
      // the markdown body so a Tailwind edit cannot silently unbind this.
      scrollRoot: document.querySelector('.msg-content')!.parentElement as HTMLElement,
    }
  }

  it('flashes the comment row when its highlighted anchor is clicked', async () => {
    const { painted, scrollRoot } = await mountWithPaintedComment()
    caretInside(painted)
    await act(async () => { fireEvent.click(scrollRoot, { clientX: 20, clientY: 20 }) })
    const row = document.querySelector('[data-comment-id="c1"]') as HTMLElement
    // The background is set through a CSS custom property, which happy-dom
    // does not retain on the inline style; the transition it is paired with is
    // a plain value and proves the same code ran.
    expect(row.style.transition).toBe('background 0.3s ease')
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled()

    // A second click cancels the in-flight flash before starting the next one,
    // so two rows are never lit at once.
    await act(async () => { fireEvent.click(scrollRoot, { clientX: 20, clientY: 20 }) })
    expect(vi.mocked(Element.prototype.scrollIntoView).mock.calls.length).toBeGreaterThan(1)
  })

  it('ignores a pointer that lands outside every commented range', async () => {
    const { scrollRoot } = await mountWithPaintedComment()
    // A caret in a detached node cannot be inside any painted range.
    const stray = document.createTextNode('elsewhere')
    Object.defineProperty(document, 'caretRangeFromPoint', {
      configurable: true,
      value: () => { const r = document.createRange(); r.setStart(stray, 0); r.collapse(true); return r },
    })
    await act(async () => { fireEvent.mouseMove(scrollRoot, { clientX: 5, clientY: 5 }) })
    expect(document.querySelector('.mc-comment-tooltip')).toBeNull()
    await act(async () => { fireEvent.click(scrollRoot, { clientX: 5, clientY: 5 }) })
    const row = document.querySelector('[data-comment-id="c1"]') as HTMLElement
    expect(row.style.transition).toBe('')
  })

  it('repaints the highlights after the preview DOM mutates underneath them', async () => {
    const { scrollRoot } = await mountWithPaintedComment()
    // Lazy content and syntax highlighting land after the first paint; the
    // observer is what keeps the ranges attached to the new nodes.
    await act(async () => {
      scrollRoot.appendChild(document.createElement('span'))
      await new Promise(resolve => setTimeout(resolve, 120))
    })
    expect(highlightRegistry.get('mc-comment')?.length).toBe(1)
  })

  it('adopts the drafts of a file switched in under the same panel', async () => {
    localStorage.setItem('mc-comment-drafts', JSON.stringify({
      '/tmp/first.md': [{ id: 'a1', anchor: 'alpha', text: 'note on the first file' }],
      '/tmp/second.md': [{ id: 'b1', anchor: 'alpha', text: 'note on the second file' }],
    }))
    const onSubmitComments = vi.fn()
    const { rerender } = render(
      <MarkdownPanel embedded {...panelProps({ filePath: '/tmp/first.md', content: BODY, onSubmitComments })} />,
      { wrapper },
    )
    expect(await screen.findByText('note on the first file')).toBeInTheDocument()
    rerender(
      <MarkdownPanel embedded {...panelProps({ filePath: '/tmp/second.md', content: BODY, onSubmitComments })} />,
    )
    expect(await screen.findByText('note on the second file')).toBeInTheDocument()
    expect(screen.queryByText('note on the first file')).toBeNull()
  })
})

/**
 * The diff surface has two shapes, and the panel picks between them:
 *
 *   diff + preview  -> `PierreFilePair`, view-only. Nothing here can receive an
 *                      edit, which is the property the old suite proved by
 *                      firing Monaco's change event and asserting silence.
 *   diff + source   -> the editor, seeded with `diffBase` so the buffer is
 *                      diffed against HEAD while it is typed into.
 *
 * Everything else the old Monaco tests reached — theme registration, the jump to
 * the first change, the selection→comment bridge — is now Pierre's own business
 * inside its shadow root, and is asserted in Playwright rather than mocked back
 * into existence here.
 */
describe('MarkdownPanel — diff surface wiring', () => {
  it('renders the diff read-only in preview, with no editor to type into', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue({ diff: 'x', original: 'from HEAD\n', status: 'clean' } as never)
    mountPanel({ initialDiffMode: true, content: 'in the buffer\n' })
    expect(await screen.findByTestId('pierre-diff')).toBeInTheDocument()
    expect(screen.queryByTestId('pierre-editor')).toBeNull()
  })

  it('swaps the read-only diff for an editor diffed against HEAD in source mode', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue({ diff: 'x', original: 'from HEAD\n', status: 'clean' } as never)
    mountPanel({ initialDiffMode: true, content: 'in the buffer\n' })
    await screen.findByTestId('pierre-diff')
    fireEvent.click(screen.getByText('Edit'))
    const editor = await screen.findByTestId('pierre-editor')
    expect(screen.queryByTestId('pierre-diff')).toBeNull()
    // `diffBase` is what makes it the live-diff surface rather than the plain
    // editor — an `undefined` here silently drops the whole diff-while-editing
    // feature with no other visible symptom.
    expect(editor).toHaveAttribute('data-diff-base', 'from HEAD\n')
    expect(editor).toHaveAttribute('data-value', 'in the buffer\n')
  })

  it('forwards an edit made on the editable diff surface to the owner', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue({ diff: 'x', original: 'from HEAD\n', status: 'clean' } as never)
    const onContentChange = vi.fn()
    mountPanel({ initialDiffMode: true, content: 'in the buffer\n', onContentChange })
    fireEvent.click(screen.getByText('Edit'))
    fireEvent.click(await screen.findByTestId('pierre-editor-emit'))
    expect(onContentChange).toHaveBeenCalledWith('edited inside pierre')
  })

  it('leaves the plain editor unseeded when the diff is off', async () => {
    mountPanel({ content: 'in the buffer\n' })
    fireEvent.click(screen.getByText('Edit'))
    const editor = await screen.findByTestId('pierre-editor')
    expect(editor).toHaveAttribute('data-diff-base', 'none')
  })

  it('shares the app-wide split/unified preference with the editable diff', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue({ diff: 'x', original: 'from HEAD\n', status: 'clean' } as never)
    localStorage.setItem('mc-diff-split', '0')
    mountPanel({ initialDiffMode: true, content: 'in the buffer\n' })
    fireEvent.click(screen.getByText('Edit'))
    expect(await screen.findByTestId('pierre-editor')).toHaveAttribute('data-diff-split', 'false')
  })
})

describe('MarkdownPanel — fullscreen for a code file', () => {
  async function goFullscreen(opts: MountOpts) {
    mountPanel(opts)
    openPanelMenu()
    fireEvent.click(screen.getByText('Full screen'))
    return screen.findByRole('dialog')
  }

  it('offers the knowledge toggle in the overlay header too', async () => {
    fetchOpts.knowledgeEnabled = true
    const dialog = await goFullscreen({})
    await waitFor(() => expect(
      within(dialog).getByLabelText('Add to Knowledge Library'),
    ).toBeInTheDocument())
  })

  it('carries the editor toolbar for a code file, which the side panel does not', async () => {
    // The side-panel header keeps only mode toggles; the overlay header is the
    // one surface that still renders the editor controls inline.
    const dialog = await goFullscreen({ filePath: '/tmp/mod.py', content: 'value = 41 + 1\n' })
    expect(within(dialog).getByLabelText('Toggle word wrap')).toBeInTheDocument()
    expect(within(dialog).getByLabelText('Toggle line numbers')).toBeInTheDocument()
    expect(within(dialog).getByLabelText('Toggle diff view')).toBeInTheDocument()
    // A code file opens straight in the editor and has no preview to offer.
    expect(within(dialog).getByTestId('pierre-editor')).toBeInTheDocument()
    expect(within(dialog).queryByText('Preview')).toBeNull()
  })

  it('persists the overlay editor toggles from the overlay itself', async () => {
    const dialog = await goFullscreen({ filePath: '/tmp/mod.py', content: 'value = 41 + 1\n' })
    fireEvent.click(within(dialog).getByLabelText('Toggle word wrap'))
    fireEvent.click(within(dialog).getByLabelText('Toggle line numbers'))
    await waitFor(() => expect(localStorage.getItem('mc-file-wordwrap')).toBe('0'))
    expect(localStorage.getItem('mc-file-linenums')).toBe('0')
  })

  it('flips a markdown file between preview and edit from the overlay toolbar', async () => {
    // `canPreview` is markdown-only, so this Edit/Preview pair is the overlay's
    // copy of the side-panel toggle rather than an extra route for code files.
    const dialog = await goFullscreen({ content: '# Title\n\nalpha beta gamma\n' })
    fireEvent.click(within(dialog).getByText('Edit'))
    expect(await within(dialog).findByTestId('pierre-editor')).toBeInTheDocument()
    fireEvent.click(within(dialog).getByText('Preview'))
    await waitFor(() => expect(within(dialog).queryByTestId('pierre-editor')).toBeNull())
  })

  it('copies the path from the overlay footer', async () => {
    const dialog = await goFullscreen({})
    fireEvent.click(within(dialog).getByTitle('Click to copy path'))
    expect(copyToClipboard).toHaveBeenCalledWith('/tmp/notes.md')
  })
})

describe('MarkdownPanel — live file watch', () => {
  interface StubStream { onmessage?: (ev: { data: string }) => void; onerror?: () => void; onopen?: () => void }
  const streams: StubStream[] = []

  function installEventSource() {
    class StubEventSource implements StubStream {
      onmessage?: (ev: { data: string }) => void
      onerror?: () => void
      onopen?: () => void
      constructor(readonly url: string) { streams.push(this) }
      close() {}
    }
    vi.stubGlobal('EventSource', StubEventSource)
  }

  it('pushes a watched on-disk change into the panel', async () => {
    streams.length = 0
    installEventSource()
    const onContentChange = vi.fn()
    const props = { ...panelProps({ onContentChange }), liveWatch: true }
    render(<MarkdownPanel embedded {...props} />, { wrapper })
    await waitFor(() => expect(streams.length).toBe(1))
    act(() => { streams[0].onmessage?.({ data: JSON.stringify({ content: 'rewritten on disk' }) }) })
    expect(onContentChange).toHaveBeenCalledWith('rewritten on disk')
  })

  it('does not watch a dirty buffer, which a disk push would clobber', async () => {
    streams.length = 0
    installEventSource()
    const props = { ...panelProps({ content: 'edited body', savedBaseline: 'disk body' }), liveWatch: true }
    render(<MarkdownPanel embedded {...props} />, { wrapper })
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(streams.length).toBe(0)
  })
})

describe('MarkdownPanel — comment anchoring edge cases', () => {
  /** Select `word` in the preview and raise the selection toolbar. */
  async function selectInPreview(word: string) {
    const para = await screen.findByText(/alpha beta gamma/)
    const textNode = para.firstChild as Text
    const start = textNode.data.indexOf(word)
    const range = document.createRange()
    range.setStart(textNode, start)
    range.setEnd(textNode, start + word.length)
    const sel = window.getSelection()!
    sel.removeAllRanges()
    sel.addRange(range)
    fireEvent.mouseUp(document)
    return screen.findByRole('button', { name: 'Comment' })
  }

  it('recovers the anchor from the toolbar text when the selection is already gone', async () => {
    mountPanel({ onSubmitComments: vi.fn() })
    const commentBtn = await selectInPreview('gamma')
    // A click elsewhere can clear the live selection before the handler runs;
    // the anchor then has to come from the text the toolbar captured.
    window.getSelection()!.removeAllRanges()
    fireEvent.click(commentBtn)
    fireEvent.change(await screen.findByLabelText('Add a comment'), { target: { value: 'from the fallback' } })
    fireEvent.click(screen.getByLabelText('Add comment'))
    await screen.findByText('from the fallback')
    const stored = JSON.parse(localStorage.getItem('mc-comment-drafts') || '{}')
    expect(stored['/tmp/notes.md'][0]).toMatchObject({ anchor: 'gamma', line: 3, column: 12 })
    // No live range existed, so nothing was wrapped in a <mark>.
    expect(document.querySelector('mark')).toBeNull()
  })

  it('marks every text node a cross-block selection touches', async () => {
    mountPanel({ content: '# Title\n\nalpha beta gamma\n\ndelta epsilon\n', onSubmitComments: vi.fn() })
    const first = await screen.findByText(/alpha beta gamma/)
    const second = await screen.findByText(/delta epsilon/)
    const range = document.createRange()
    range.setStart(first.firstChild as Text, 6)
    range.setEnd(second.firstChild as Text, 5)
    const sel = window.getSelection()!
    sel.removeAllRanges()
    sel.addRange(range)
    fireEvent.mouseUp(document)
    fireEvent.click(await screen.findByRole('button', { name: 'Comment' }))
    await screen.findByLabelText('Add a comment')
    // One <mark> per touched text node, not one for the whole selection.
    expect(document.querySelectorAll('mark').length).toBeGreaterThan(1)
  })
})
