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
 *   - the Monaco diff editor's `beforeMount` / `onMount` wiring (theme
 *     registration, edit forwarding, and the selection → comment hand-off),
 *   - the comment-highlight click → row flash, and the pointer miss,
 *   - fullscreen for a CODE file, where the overlay toolbar is the only route
 *     to preview mode and therefore to the hljs render.
 *
 * `Highlight` / `CSS.highlights` are stubbed BEFORE the dynamic import because
 * MarkdownPanel captures both into module-level constants at load time. Monaco
 * is mocked, but unlike the sibling suites this mock DRIVES the editor
 * callbacks: the component's whole diff integration lives inside them, and a
 * stub that renders a bare div leaves every line of it unexecuted.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useEffect } from 'react'
import { render, screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

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

// ── Monaco diff editor stub that actually invokes the panel's callbacks ──────
interface ModifiedStub {
  onDidChangeModelContent: (cb: () => void) => { dispose: () => void }
  onMouseUp: (cb: () => void) => { dispose: () => void }
  getValue: () => string
  getSelection: () => { isEmpty: () => boolean; getEndPosition: () => unknown } | null
  getModel: () => { getValueInRange: () => string | undefined }
  getScrolledVisiblePosition: () => { left: number; top: number; height: number } | null
  getDomNode: () => HTMLElement | null
  revealLineInCenter: (line: number) => void
}
interface EditorStub {
  onDidUpdateDiff: (cb: () => void) => { dispose: () => void }
  getLineChanges: () => { modifiedStartLineNumber: number }[]
  getModifiedEditor: () => ModifiedStub
}

const monaco = {
  defineTheme: vi.fn(),
  revealLineInCenter: vi.fn(),
  /** Value the modified editor reports — what an edit forwards to the owner. */
  modifiedValue: 'edited inside monaco',
  /** null = the mouse-up left no selection behind. */
  selectionText: null as string | null,
  updateDiff: undefined as undefined | (() => void),
  contentChange: undefined as undefined | (() => void),
  mouseUp: undefined as undefined | (() => void),
}

function makeEditorStub(): EditorStub {
  const domNode = document.createElement('div')
  const modified: ModifiedStub = {
    onDidChangeModelContent: (cb) => { monaco.contentChange = cb; return { dispose: () => {} } },
    onMouseUp: (cb) => { monaco.mouseUp = cb; return { dispose: () => {} } },
    getValue: () => monaco.modifiedValue,
    getSelection: () => (monaco.selectionText === null
      ? null
      : { isEmpty: () => false, getEndPosition: () => ({ lineNumber: 1, column: 1 }) }),
    getModel: () => ({ getValueInRange: () => monaco.selectionText ?? undefined }),
    getScrolledVisiblePosition: () => ({ left: 12, top: 20, height: 16 }),
    getDomNode: () => domNode,
    revealLineInCenter: monaco.revealLineInCenter,
  }
  return {
    onDidUpdateDiff: (cb) => { monaco.updateDiff = cb; return { dispose: () => {} } },
    getLineChanges: () => [{ modifiedStartLineNumber: 4 }],
    getModifiedEditor: () => modified,
  }
}

vi.mock('@monaco-editor/react', () => ({
  default: ({ value }: { value?: string }) => <div data-testid="monaco" data-value={value} />,
  DiffEditor: ({ beforeMount, onMount, modified }: {
    beforeMount?: (m: { editor: { defineTheme: (n: string, t: unknown) => void } }) => void
    onMount?: (e: EditorStub) => void
    modified?: string
  }) => {
    useEffect(() => {
      beforeMount?.({ editor: { defineTheme: monaco.defineTheme } })
      onMount?.(makeEditorStub())
      // The panel keeps the callbacks in refs, so re-running on every prop
      // change would re-register listeners the component never disposes.
      // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-only, mirrors Monaco's own contract
    }, [])
    return <div data-testid="monaco-diff" data-modified={modified} />
  },
  loader: { config: () => {} },
}))
vi.mock('monaco-editor', () => ({}))
vi.mock('../utils/monacoLocal', () => ({ ensureMonacoLocal: async () => {} }))

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
  monaco.modifiedValue = 'edited inside monaco'
  monaco.selectionText = null
  monaco.updateDiff = undefined
  monaco.contentChange = undefined
  monaco.mouseUp = undefined
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
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const onContentChange = vi.fn()
    fetchOpts.fileReadText = 'the version on disk'
    mountPanel({ content: 'edited body', savedBaseline: 'disk body', onContentChange })
    fireEvent.click(screen.getByText('View Source'))
    fireEvent.click(screen.getByText('Cancel'))
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
      scrollRoot: document.querySelector('.overflow-auto') as HTMLElement,
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

describe('MarkdownPanel — Monaco diff wiring', () => {
  it('registers both editor themes exactly once and jumps to the first change', async () => {
    mountPanel({ initialDiffMode: true })
    await screen.findByTestId('monaco-diff')
    const themes = monaco.defineTheme.mock.calls.map(([name]) => name)
    expect(themes).toEqual(['kirocrew-dark', 'kirocrew-light'])

    act(() => { monaco.updateDiff!() })
    expect(monaco.revealLineInCenter).toHaveBeenCalledWith(4)
  })

  it('forwards a Monaco edit to the owner only while the diff is editable', async () => {
    const onContentChange = vi.fn()
    mountPanel({ initialDiffMode: true, onContentChange })
    await screen.findByTestId('monaco-diff')

    // Preview-side diff is read-only: an edit event cannot reach the owner.
    act(() => { monaco.contentChange!() })
    expect(onContentChange).not.toHaveBeenCalled()

    fireEvent.click(screen.getByText('View Source'))
    act(() => { monaco.contentChange!() })
    expect(onContentChange).toHaveBeenCalledWith('edited inside monaco')
  })

  it('raises the comment popover from a diff-editor selection', async () => {
    monaco.selectionText = '  const answer = 42  '
    mountPanel({ initialDiffMode: true, onSubmitComments: vi.fn() })
    await screen.findByTestId('monaco-diff')

    // The panel debounces the selection read by 10ms so Monaco has committed it.
    await act(async () => {
      monaco.mouseUp!()
      await new Promise(resolve => setTimeout(resolve, 30))
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Comment' }))
    // No DOM selection exists on the Monaco path, so the rect is used as-is.
    expect(await screen.findByLabelText('Add a comment')).toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('Close'))
    await waitFor(() => expect(screen.queryByLabelText('Add a comment')).toBeNull())
  })

  it('leaves the selection alone when the mouse-up selected nothing', async () => {
    mountPanel({ initialDiffMode: true, onSubmitComments: vi.fn() })
    await screen.findByTestId('monaco-diff')
    await act(async () => {
      monaco.mouseUp!()
      await new Promise(resolve => setTimeout(resolve, 30))
    })
    expect(screen.queryByRole('button', { name: 'Comment' })).toBeNull()
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

  it('is the only route from a code file to its highlighted preview', async () => {
    // canPreview is markdown-only, so the side-panel header has no
    // source/preview toggle for a .py file — the overlay toolbar carries one.
    const dialog = await goFullscreen({ filePath: '/tmp/mod.py', content: 'value = 41 + 1\n' })
    expect(within(dialog).getByLabelText('Toggle word wrap')).toBeInTheDocument()
    expect(within(dialog).getByLabelText('Toggle autocomplete')).toBeInTheDocument()
    expect(within(dialog).getByLabelText('Toggle line numbers')).toBeInTheDocument()

    fireEvent.click(within(dialog).getByText('Preview'))
    // Out of the editor: the code is now rendered read-only and syntax-tokenised.
    await waitFor(() => expect(within(dialog).getByText('Edit')).toBeInTheDocument())
    const pre = dialog.querySelector('pre')
    expect(pre).not.toBeNull()
    // Only the tokenised spans are asserted: DOMPurify running on happy-dom
    // drops the leading bare text node of a fragment, so `value =` is absent
    // here while a real browser keeps it — an environment artifact, not the
    // panel's behaviour.
    expect(pre!.querySelector('.hljs-number')).not.toBeNull()
    expect(pre!.textContent).toContain('41 + 1')
  })

  it('persists the overlay editor toggles from the overlay itself', async () => {
    const dialog = await goFullscreen({ filePath: '/tmp/mod.py', content: 'value = 41 + 1\n' })
    fireEvent.click(within(dialog).getByLabelText('Toggle word wrap'))
    fireEvent.click(within(dialog).getByLabelText('Toggle autocomplete'))
    fireEvent.click(within(dialog).getByLabelText('Toggle line numbers'))
    await waitFor(() => expect(localStorage.getItem('mc-file-wordwrap')).toBe('0'))
    expect(localStorage.getItem('mc-file-autocomplete')).toBe('0')
    expect(localStorage.getItem('mc-file-linenums')).toBe('0')
  })

  it('falls back to auto-detection for an extension with no registered grammar', async () => {
    // langFor maps an unknown extension to `plaintext`, which this build never
    // registers with highlight.js — so the explicit-language call throws and the
    // auto-detecting pass has to render the file instead of it going blank.
    const dialog = await goFullscreen({ filePath: '/tmp/gateway.log', content: 'READY on port 8080\n' })
    fireEvent.click(within(dialog).getByText('Preview'))
    await waitFor(() => expect(within(dialog).getByText('Edit')).toBeInTheDocument())
    const pre = dialog.querySelector('pre')
    expect(pre).not.toBeNull()
    expect(pre!.textContent).toContain('8080')
  })

  it('copies the path from the overlay footer', async () => {    const dialog = await goFullscreen({})
    fireEvent.click(within(dialog).getByTitle('Click to copy path'))
    expect(copyToClipboard).toHaveBeenCalledWith('/tmp/notes.md')
  })

  it('routes a diff-editor selection into the overlay comment toolbar', async () => {
    monaco.selectionText = 'alpha beta'
    await goFullscreen({ initialDiffMode: true, onSubmitComments: vi.fn() })
    await screen.findByTestId('monaco-diff')
    await act(async () => {
      monaco.mouseUp!()
      await new Promise(resolve => setTimeout(resolve, 30))
    })
    expect(await screen.findByRole('button', { name: 'Comment' })).toBeInTheDocument()
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
