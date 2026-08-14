/**
 * Cold-path tests for `MarkdownPanel`.
 *
 * The existing suites cover the ⋯ menu inventory and the reveal-forces-source
 * rule. This one aims at what they never enter: the exported source-position
 * resolvers, the Download hand-off, the panel's refresh / save / cancel /
 * diff / fullscreen chrome, the preview find bar, the artifact + knowledge
 * promotion mutations, and the CSS-Custom-Highlight comment overlay.
 *
 * `Highlight` and `CSS.highlights` are stubbed BEFORE the module is imported,
 * because MarkdownPanel captures both into module-level constants at load time.
 * happy-dom ships neither, so without the stub `FIND_HL_SUPPORTED` is false and
 * every highlight path is unreachable. Monaco is mocked for the same reason the
 * reveal-line suite mocks it: it is lazy, heavy, and does not lay out here.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
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

vi.mock('@monaco-editor/react', () => ({
  default: ({ value, language }: { value?: string; language?: string }) => (
    <div data-testid="monaco" data-language={language} data-value={value} />
  ),
  DiffEditor: () => <div data-testid="monaco-diff" />,
  loader: { config: () => {} },
}))
vi.mock('monaco-editor', () => ({}))
vi.mock('../utils/monacoLocal', () => ({ ensureMonacoLocal: async () => {} }))

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
const { default: MarkdownPanel, OverflowMenu, resolveSourcePos, findCoords } =
  await import('../components/MarkdownPanel')

// ── fetch router ────────────────────────────────────────────────────────────
interface FetchOpts {
  knowledgeEnabled?: boolean
  knowledgeAdded?: boolean
  knowledgePostStatus?: number
  fileReadOk?: boolean
  fileReadText?: string
  fileReadTruncated?: boolean
  downloadOk?: boolean
  downloadThrows?: boolean
}
let fetchOpts: FetchOpts = {}

function installFetch() {
  vi.stubGlobal('fetch', vi.fn(async (input: unknown, init?: { method?: string }) => {
    const url = String(input)
    if (url.startsWith('/api/knowledge/config')) {
      return { ok: true, json: async () => ({ enabled: !!fetchOpts.knowledgeEnabled, supported_formats: ['.md', '.txt'] }) }
    }
    if (url.startsWith('/api/knowledge/sources')) {
      if (init?.method === 'POST') {
        const status = fetchOpts.knowledgePostStatus ?? 201
        return { ok: status < 400, status, json: async () => (status >= 400 ? { error: 'library refused' } : { id: 1 }) }
      }
      return { ok: true, json: async () => (fetchOpts.knowledgeAdded ? [{ id: 1 }] : []) }
    }
    if (url.startsWith('/api/file-download')) {
      if (fetchOpts.downloadThrows) throw new Error('network down')
      if (fetchOpts.downloadOk === false) return { ok: false, status: 500, statusText: 'Server Error' }
      return { ok: true, blob: async () => new Blob(['bytes']) }
    }
    // /api/file-read
    const ok = fetchOpts.fileReadOk !== false
    return {
      ok,
      status: ok ? 200 : 404,
      headers: { get: (k: string) => (k === 'X-Truncated' && fetchOpts.fileReadTruncated ? 'true' : null) },
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
  // The api mocks are module-factory `vi.fn()`s, so `restoreAllMocks` leaves
  // their call logs behind — one test's createArtifact call would otherwise be
  // read as the next test's.
  vi.clearAllMocks()
  qc.clear()
  fetchOpts = {}
  highlightRegistry.clear()
  localStorage.clear()
  document.getElementById('mc-comment-hl-style')?.remove()
  installFetch()
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
})

// ════════════════════════════════════════════════════════════════════════════
// resolveSourcePos — the DOM-to-source coordinate resolver
// ════════════════════════════════════════════════════════════════════════════

/** Build a detached tree from HTML (ACAT-safe, no innerHTML). */
function tree(html: string): HTMLElement {
  const root = document.createElement('div')
  root.appendChild(document.createRange().createContextualFragment(html))
  return root
}

/** A range starting `offset` chars into the `nth` text node under `root`. */
function rangeAt(root: HTMLElement, offset: number, nth = 0): Range {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const nodes: Text[] = []
  let n: Node | null
  while ((n = walker.nextNode())) nodes.push(n as Text)
  const target = nodes[nth]
  const r = document.createRange()
  r.setStart(target, offset)
  r.setEnd(target, Math.min(offset + 1, target.data.length))
  return r
}

describe('resolveSourcePos', () => {
  it('returns undefined when no ancestor carries a source position', () => {
    const root = tree('<p>hello world</p>')
    expect(resolveSourcePos(rangeAt(root, 6), root, 'hello world')).toBeUndefined()
  })

  it('returns undefined for an unparseable data-sourcepos attribute', () => {
    const root = tree('<p data-sourcepos="not-a-position">hello world</p>')
    expect(resolveSourcePos(rangeAt(root, 6), root, 'hello world')).toBeUndefined()
  })

  it('resolves a plain single-line selection to its 1-based column', () => {
    const root = tree('<p data-sourcepos="1:1-1:12">hello world</p>')
    expect(resolveSourcePos(rangeAt(root, 6), root, 'hello world')).toEqual({ line: 1, column: 7 })
  })

  it('skips markdown syntax characters that have no rendered counterpart', () => {
    // Rendered text is "bold text"; the source is "**bold** text", so the
    // selection at rendered offset 5 must land on source column 10 — the
    // alignment walk has to step over the four asterisks.
    const root = tree('<p data-sourcepos="1:1-1:14"><strong>bold</strong> text</p>')
    expect(resolveSourcePos(rangeAt(root, 1, 1), root, '**bold** text')).toEqual({ line: 1, column: 10 })
  })

  it('offsets the line by the enclosing block start', () => {
    // useBlockAssembler numbers data-sourcepos relative to the block, so the
    // [data-block-start] wrapper is what makes the coordinate absolute.
    const root = tree('<div data-block-start="5"><p data-sourcepos="1:1-1:6">hello</p></div>')
    expect(resolveSourcePos(rangeAt(root, 2), root, 'a\nb\nc\nd\nhello\n')).toEqual({ line: 5, column: 3 })
  })

  it('crosses a newline inside a multi-line source span', () => {
    const root = tree('<p data-sourcepos="1:1-2:9">line one\nline two</p>')
    expect(resolveSourcePos(rangeAt(root, 9), root, 'line one\nline two\n')).toEqual({ line: 2, column: 1 })
  })

  it('falls back to the element start when the span runs past the content', () => {
    const root = tree('<p data-sourcepos="1:1-9:3">hi</p>')
    expect(resolveSourcePos(rangeAt(root, 1), root, 'hi')).toEqual({ line: 1, column: 1 })
  })

  it('falls back to the element start when the offset is past the rendered text', () => {
    const root = tree('<p data-sourcepos="1:1-1:4">abc</p>')
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
    const text = walker.nextNode() as Text
    const r = document.createRange()
    r.setStart(text, 3)
    r.setEnd(text, 3)
    expect(resolveSourcePos(r, root, 'abc')).toEqual({ line: 1, column: 1 })
  })

  it('returns undefined when a rendered character has no source counterpart', () => {
    // `&nbsp;` renders as U+00A0, which does not appear in the source span at
    // all — the alignment walk exhausts the span and must NOT guess a column.
    const root = tree('<p data-sourcepos="1:1-1:9">x&nbsp;Q</p>')
    expect(resolveSourcePos(rangeAt(root, 1), root, 'x&nbsp;Q')).toBeUndefined()
  })
})

describe('findCoords', () => {
  it('returns undefined for an empty needle', () => {
    expect(findCoords('alpha', '')).toBeUndefined()
  })

  it('returns undefined when the needle is absent', () => {
    expect(findCoords('alpha', 'omega')).toBeUndefined()
  })

  it('reports 1-based coordinates for a first-line hit', () => {
    expect(findCoords('alpha beta', 'beta')).toEqual({ line: 1, column: 7 })
  })

  it('reports the column relative to the start of a later line', () => {
    expect(findCoords('one\ntwo three\n', 'three')).toEqual({ line: 2, column: 5 })
  })
})

// ════════════════════════════════════════════════════════════════════════════
// OverflowMenu — the Download hand-off
// ════════════════════════════════════════════════════════════════════════════

function openOverflow(filePath = '/tmp/notes.md', content = '# hi\n') {
  render(<OverflowMenu filePath={filePath} content={content} />, { wrapper })
  fireEvent.click(screen.getByTestId('markdown-panel-more-options'))
}

describe('OverflowMenu Download', () => {
  it('streams the raw bytes through the file-download endpoint', async () => {
    openOverflow()
    fireEvent.click(screen.getByText('Download'))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      '/api/file-download?path=%2Ftmp%2Fnotes.md',
    ))
    expect(window.alert).not.toHaveBeenCalled()
  })

  it('alerts instead of writing a zero-byte file when the endpoint refuses', async () => {
    fetchOpts.downloadOk = false
    openOverflow()
    fireEvent.click(screen.getByText('Download'))
    await waitFor(() => expect(window.alert).toHaveBeenCalledWith('Download failed'))
  })

  it('alerts when the download request throws outright', async () => {
    fetchOpts.downloadThrows = true
    openOverflow()
    fireEvent.click(screen.getByText('Download'))
    await waitFor(() => expect(window.alert).toHaveBeenCalledWith('Download failed'))
  })
})

// ════════════════════════════════════════════════════════════════════════════
// The panel itself
// ════════════════════════════════════════════════════════════════════════════

interface MountOpts {
  filePath?: string
  content?: string
  savedBaseline?: string
  onRefresh?: (p: string) => Promise<void>
  onSave?: (p: string, c: string) => Promise<void>
  onClose?: () => void
  onContentChange?: (c: string) => void
  onDiffModeChange?: (d: boolean) => void
  onOpenFolder?: (p: string) => void
  onSubmitComments?: (m: string) => void
  /** Omit the key entirely to let the auto-diff heuristic decide. */
  initialDiffMode?: boolean
}

function mountPanel(opts: MountOpts = {}) {
  const props = {
    filePath: opts.filePath ?? '/tmp/notes.md',
    content: opts.content ?? '# Title\n\nalpha beta alpha\n',
    onContentChange: opts.onContentChange ?? vi.fn(),
    onSave: opts.onSave ?? vi.fn(async () => {}),
    onClose: opts.onClose ?? vi.fn(),
    onRefresh: opts.onRefresh,
    onDiffModeChange: opts.onDiffModeChange,
    onOpenFolder: opts.onOpenFolder,
    onSubmitComments: opts.onSubmitComments,
    savedBaseline: opts.savedBaseline,
    initialDiffMode: 'initialDiffMode' in opts ? opts.initialDiffMode : false,
  }
  const utils = render(<MarkdownPanel embedded {...props} />, { wrapper })
  return { ...utils, props }
}

/** Open the panel's ⋯ menu (the first one — the header's, not fullscreen's). */
function openPanelMenu() {
  fireEvent.click(screen.getAllByTestId('markdown-panel-more-options')[0])
}

describe('MarkdownPanel — refresh', () => {
  it('delegates Refresh to the owner when onRefresh is supplied', async () => {
    const onRefresh = vi.fn(async () => {})
    mountPanel({ onRefresh })
    openPanelMenu()
    fireEvent.click(screen.getByText('Refresh'))
    await waitFor(() => expect(onRefresh).toHaveBeenCalledWith('/tmp/notes.md'))
  })

  it('re-reads the file itself when no owner refresh is supplied', async () => {
    const onContentChange = vi.fn()
    fetchOpts.fileReadText = 'reloaded from disk'
    mountPanel({ onContentChange })
    openPanelMenu()
    fireEvent.click(screen.getByText('Refresh'))
    await waitFor(() => expect(onContentChange).toHaveBeenCalledWith('reloaded from disk'))
  })

  it('disables Refresh while the buffer is dirty so edits cannot be clobbered', () => {
    mountPanel({ content: 'edited', savedBaseline: 'on disk' })
    openPanelMenu()
    expect(screen.getByText('Refresh').closest('button')).toBeDisabled()
  })
})

describe('MarkdownPanel — save and cancel', () => {
  /** A dirty markdown buffer switched into source mode, where Save/Cancel live. */
  function mountDirtySource(opts: MountOpts = {}) {
    const r = mountPanel({ content: 'edited body', savedBaseline: 'disk body', ...opts })
    fireEvent.click(screen.getByText('View Source'))
    return r
  }

  it('marks the buffer dirty from a restored draft that differs from disk', () => {
    mountPanel({ content: 'edited body', savedBaseline: 'disk body' })
    expect(screen.getByTitle('Unsaved changes')).toBeInTheDocument()
  })

  it('hands the current buffer to the owner on Save', async () => {
    const onSave = vi.fn(async () => {})
    mountDirtySource({ onSave })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(onSave).toHaveBeenCalledWith('/tmp/notes.md', 'edited body'))
  })

  it('surfaces the failure message when the save is rejected', async () => {
    const onSave = vi.fn(async () => { throw new Error('disk is read-only') })
    mountDirtySource({ onSave })
    fireEvent.click(screen.getByText('Save'))
    expect(await screen.findByText('disk is read-only')).toBeInTheDocument()
  })

  it('re-reads from disk on Cancel once the discard is confirmed', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const onRefresh = vi.fn(async () => {})
    mountDirtySource({ onRefresh })
    fireEvent.click(screen.getByText('Cancel'))
    await waitFor(() => expect(onRefresh).toHaveBeenCalledWith('/tmp/notes.md'))
    expect(window.confirm).toHaveBeenCalledWith('Discard unsaved changes?')
  })

  it('keeps the edits when the discard confirmation is declined', () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const onRefresh = vi.fn(async () => {})
    mountDirtySource({ onRefresh })
    fireEvent.click(screen.getByText('Cancel'))
    expect(onRefresh).not.toHaveBeenCalled()
    expect(screen.getByText('Save')).toBeInTheDocument()
  })

  it('routes Escape through the same discard guard as the close button', () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const onClose = vi.fn()
    mountPanel({ content: 'edited body', savedBaseline: 'disk body', onClose })
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
    vi.mocked(window.confirm).mockReturnValue(true)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('saves on Cmd+S while editing a dirty buffer', async () => {
    const onSave = vi.fn(async () => {})
    mountDirtySource({ onSave })
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    await waitFor(() => expect(onSave).toHaveBeenCalledOnce())
  })
})

describe('MarkdownPanel — diff chrome', () => {
  it('reports the diff toggle to the owner and shows the +/- line stats', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue({ diff: 'x', original: 'one\ntwo\n', status: 'clean' } as never)
    const onDiffModeChange = vi.fn()
    // No initialDiffMode: an unmodified file leaves the auto-diff effect inert,
    // so it cannot race the click below back to preview.
    mountPanel({ content: 'one\ntwo\nthree\n', onDiffModeChange, initialDiffMode: undefined })
    await waitFor(() => expect(api.fileDiff).toHaveBeenCalled())
    fireEvent.click(screen.getAllByLabelText('Toggle diff view')[0])
    expect(onDiffModeChange).toHaveBeenCalledWith(true)
    expect(await screen.findByText('+1')).toBeInTheDocument()
  })

  it('flips the split/unified control only once a diff is on screen', async () => {
    mountPanel({ initialDiffMode: undefined })
    await waitFor(() => expect(api.fileDiff).toHaveBeenCalled())
    expect(screen.queryByLabelText('Switch to unified view')).toBeNull()
    fireEvent.click(screen.getAllByLabelText('Toggle diff view')[0])
    const split = screen.getByLabelText('Switch to unified view')
    expect(split).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(split)
    expect(screen.getByLabelText('Switch to split view')).toHaveAttribute('aria-pressed', 'false')
  })

  it('adopts the owner-restored diff preference over the auto-diff heuristic', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue({ diff: 'x', original: 'a\n', status: 'modified' } as never)
    mountPanel()
    await waitFor(() => expect(api.fileDiff).toHaveBeenCalled())
    // initialDiffMode={false} is an explicit choice; a modified file must not
    // re-open in diff mode against it.
    await waitFor(() => expect(screen.getAllByLabelText('Toggle diff view')[0]).toHaveAttribute('aria-pressed', 'false'))
  })

  it('auto-opens diff for a modified file when the owner has no stored choice', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue({ diff: 'x', original: 'a\n', status: 'modified' } as never)
    const onDiffModeChange = vi.fn()
    mountPanel({ initialDiffMode: undefined, onDiffModeChange })
    await waitFor(() => expect(onDiffModeChange).toHaveBeenCalledWith(true))
    expect(screen.getAllByLabelText('Toggle diff view')[0]).toHaveAttribute('aria-pressed', 'true')
  })
})

describe('MarkdownPanel — breadcrumb', () => {
  it('opens a clicked ancestor directory at its absolute path', () => {
    const onOpenFolder = vi.fn()
    mountPanel({ filePath: '/home/dev/docs/notes.md', onOpenFolder })
    fireEvent.click(screen.getByTitle('Open folder /home/dev'))
    expect(onOpenFolder).toHaveBeenCalledWith('/home/dev')
  })

  it('leaves the breadcrumb inert when the host has no folder surface', () => {
    mountPanel({ filePath: '/home/dev/docs/notes.md' })
    expect(screen.queryByTitle('Open folder /home/dev')).toBeNull()
    expect(screen.getByText('notes.md')).toBeInTheDocument()
  })
})

describe('MarkdownPanel — fullscreen overlay', () => {
  async function goFullscreen() {
    mountPanel()
    openPanelMenu()
    fireEvent.click(screen.getByText('Full screen'))
    return screen.findByRole('dialog')
  }

  it('renders the file in a modal dialog and locks body scroll', async () => {
    const dialog = await goFullscreen()
    expect(dialog).toHaveAttribute('aria-label', 'Full screen file preview')
    expect(document.body.style.overflow).toBe('hidden')
  })

  it('exposes the refresh control and the path footer in the overlay', async () => {
    await goFullscreen()
    expect(screen.getByLabelText('Refresh file')).toBeInTheDocument()
    expect(screen.getByTitle('Click to copy path')).toHaveTextContent('/tmp/notes.md')
  })

  it('closes the overlay and releases body scroll on Exit full screen', async () => {
    await goFullscreen()
    fireEvent.click(screen.getByLabelText('Exit full screen'))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(document.body.style.overflow).toBe('')
  })

  it('lets Escape leave fullscreen without closing the panel', async () => {
    const onClose = vi.fn()
    mountPanel({ onClose })
    openPanelMenu()
    fireEvent.click(screen.getByText('Full screen'))
    await screen.findByRole('dialog')
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(onClose).not.toHaveBeenCalled()
  })

  it('traps Tab inside the overlay at both ends of the focus ring', async () => {
    const dialog = await goFullscreen()
    const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
      'button:not([disabled]),textarea,input,a[href],select,[tabindex]:not([tabindex="-1"])',
    ))
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    expect(focusable.length).toBeGreaterThan(1)

    last.focus()
    fireEvent.keyDown(dialog, { key: 'Tab' })
    expect(document.activeElement).toBe(first)

    first.focus()
    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(last)
  })

  it('carries the editing toolbar into the overlay', async () => {
    mountPanel({ content: 'edited body', savedBaseline: 'disk body' })
    fireEvent.click(screen.getByText('View Source'))
    openPanelMenu()
    fireEvent.click(screen.getByText('Full screen'))
    const dialog = await screen.findByRole('dialog')
    // Source-mode controls exist in the overlay header, not only the side panel.
    expect(dialog.querySelector('[aria-label="Toggle word wrap"]')).not.toBeNull()
    expect(dialog.querySelector('[aria-label="Toggle autocomplete"]')).not.toBeNull()
    expect(dialog.querySelector('[aria-label="Toggle line numbers"]')).not.toBeNull()
  })
})

describe('MarkdownPanel — preview find', () => {
  /** Cmd+F only wins in markdown preview, and only for the active region. */
  async function openFind() {
    mountPanel({ content: 'alpha beta alpha gamma\n' })
    await screen.findByText(/alpha beta alpha gamma/)
    fireEvent.keyDown(document, { key: 'f', metaKey: true })
    return screen.findByLabelText('Find in document')
  }

  it('opens a find field scoped to the rendered preview', async () => {
    expect(await openFind()).toBeInTheDocument()
  })

  it('counts every match and paints them through the highlight registry', async () => {
    const input = await openFind()
    fireEvent.change(input, { target: { value: 'alpha' } })
    expect(await screen.findByText('1 of 2')).toBeInTheDocument()
    expect(highlightRegistry.has('mc-find-current')).toBe(true)
  })

  it('steps forward and backward through the matches', async () => {
    const input = await openFind()
    fireEvent.change(input, { target: { value: 'alpha' } })
    await screen.findByText('1 of 2')
    fireEvent.click(screen.getByLabelText('Next match'))
    expect(await screen.findByText('2 of 2')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Previous match'))
    expect(await screen.findByText('1 of 2')).toBeInTheDocument()
  })

  it('wraps to the last match when stepping back from the first (Shift+Enter)', async () => {
    const input = await openFind()
    fireEvent.change(input, { target: { value: 'alpha' } })
    await screen.findByText('1 of 2')
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
    expect(await screen.findByText('2 of 2')).toBeInTheDocument()
  })

  it('says so rather than showing a count when nothing matches', async () => {
    const input = await openFind()
    fireEvent.change(input, { target: { value: 'nowhere-in-here' } })
    expect(await screen.findByText('No results')).toBeInTheDocument()
  })

  it('treats the term literally, so regex metacharacters find nothing', async () => {
    const input = await openFind()
    fireEvent.change(input, { target: { value: 'al.ha' } })
    expect(await screen.findByText('No results')).toBeInTheDocument()
  })

  it('honours the case-sensitivity toggle', async () => {
    const input = await openFind()
    fireEvent.change(input, { target: { value: 'ALPHA' } })
    await screen.findByText('1 of 2')
    fireEvent.click(screen.getByLabelText('Case sensitive'))
    expect(await screen.findByText('No results')).toBeInTheDocument()
  })

  it('closes on Escape and clears the painted highlights', async () => {
    const input = await openFind()
    fireEvent.change(input, { target: { value: 'alpha' } })
    await screen.findByText('1 of 2')
    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByLabelText('Find in document')).toBeNull())
    expect(highlightRegistry.has('mc-find')).toBe(false)
  })

  it('closes on the explicit close button too', async () => {
    await openFind()
    fireEvent.click(screen.getByLabelText('Close find'))
    await waitFor(() => expect(screen.queryByLabelText('Find in document')).toBeNull())
  })

  it('hands Cmd+F back to the editor when the panel leaves preview', async () => {
    mountPanel({ content: 'alpha beta\n' })
    fireEvent.click(screen.getByText('View Source'))
    fireEvent.keyDown(document, { key: 'f', metaKey: true })
    await waitFor(() => expect(screen.queryByLabelText('Find in document')).toBeNull())
  })

  it('ignores Cmd+F for a non-markdown file, which has no preview to search', async () => {
    mountPanel({ filePath: '/tmp/mod.py', content: 'a = 1\n' })
    fireEvent.keyDown(document, { key: 'f', metaKey: true })
    await waitFor(() => expect(screen.queryByLabelText('Find in document')).toBeNull())
  })

  it('lets Cmd+F bubble to chat-find when the cursor left the panel', async () => {
    mountPanel({ content: 'alpha beta\n' })
    await screen.findByText(/alpha beta/)
    const outside = document.createElement('button')
    document.body.appendChild(outside)
    fireEvent.pointerDown(outside)
    fireEvent.keyDown(document, { key: 'f', metaKey: true })
    await waitFor(() => expect(screen.queryByLabelText('Find in document')).toBeNull())
    outside.remove()
  })
})

describe('MarkdownPanel — inline comment highlights', () => {
  const FILE = '/tmp/notes.md'
  const BODY = 'alpha beta gamma\n'

  function seedDraft(anchor: string, text = 'why this?') {
    localStorage.setItem('mc-comment-drafts', JSON.stringify({
      [FILE]: [{ id: 'c1', anchor, text }],
    }))
  }

  it('paints a persisted draft comment as a custom highlight', async () => {
    seedDraft('beta')
    mountPanel({ filePath: FILE, content: BODY, onSubmitComments: vi.fn() })
    await waitFor(() => expect(highlightRegistry.get('mc-comment')?.length).toBe(1))
    expect(document.getElementById('mc-comment-hl-style')).not.toBeNull()
  })

  it('paints nothing when the anchor text is no longer in the document', async () => {
    seedDraft('this phrase was deleted')
    mountPanel({ filePath: FILE, content: BODY, onSubmitComments: vi.fn() })
    await screen.findByText(/alpha beta gamma/)
    expect(highlightRegistry.has('mc-comment')).toBe(false)
  })

  it('drops the highlights while the file is being edited', async () => {
    seedDraft('beta')
    mountPanel({ filePath: FILE, content: BODY, onSubmitComments: vi.fn() })
    await waitFor(() => expect(highlightRegistry.has('mc-comment')).toBe(true))
    fireEvent.click(screen.getByText('View Source'))
    await waitFor(() => expect(highlightRegistry.has('mc-comment')).toBe(false))
  })

  it('shows the comment text in a follow-the-pointer tooltip over the anchor', async () => {
    seedDraft('beta', 'needs a citation')
    mountPanel({ filePath: FILE, content: BODY, onSubmitComments: vi.fn() })
    await waitFor(() => expect(highlightRegistry.get('mc-comment')?.length).toBe(1))
    const painted = highlightRegistry.get('mc-comment')![0]
    // happy-dom has no caretRangeFromPoint; supply one that lands inside the
    // painted range so the hit-test can resolve.
    Object.defineProperty(document, 'caretRangeFromPoint', {
      configurable: true,
      value: () => {
        const caret = document.createRange()
        caret.setStart(painted.startContainer, painted.startOffset)
        caret.collapse(true)
        return caret
      },
    })
    const scrollRoot = document.querySelector('.overflow-auto') as HTMLElement
    await act(async () => { fireEvent.mouseMove(scrollRoot, { clientX: 20, clientY: 20 }) })
    const tip = document.querySelector('.mc-comment-tooltip')
    expect(tip).not.toBeNull()
    expect(tip).toHaveTextContent('needs a citation')
    await act(async () => { fireEvent.mouseLeave(scrollRoot) })
    expect(document.querySelector('.mc-comment-tooltip')).toBeNull()
  })

  it('clears the registry when the panel unmounts mid-highlight', async () => {
    seedDraft('beta')
    const { unmount } = mountPanel({ filePath: FILE, content: BODY, onSubmitComments: vi.fn() })
    await waitFor(() => expect(highlightRegistry.has('mc-comment')).toBe(true))
    unmount()
    expect(highlightRegistry.has('mc-comment')).toBe(false)
  })
})

describe('MarkdownPanel — artifact promotion', () => {
  it('promotes the file by re-reading it from disk, not from the buffer', async () => {
    fetchOpts.fileReadText = '# the on-disk truth\n'
    mountPanel({ content: '# a stale in-memory copy\n' })
    fireEvent.click(await screen.findByLabelText('Add to artifact library'))
    await waitFor(() => expect(api.createArtifact).toHaveBeenCalled())
    const [payload] = vi.mocked(api.createArtifact).mock.calls[0]
    expect(payload).toMatchObject({ content: '# the on-disk truth\n', kind: 'markdown', source_path: '/tmp/notes.md' })
  })

  it('refuses to promote a truncated read rather than persisting a prefix', async () => {
    fetchOpts.fileReadTruncated = true
    mountPanel()
    fireEvent.click(await screen.findByLabelText('Add to artifact library'))
    await waitFor(() => expect(window.alert).toHaveBeenCalledWith('File is too large to add'))
    expect(api.createArtifact).not.toHaveBeenCalled()
  })

  it('reports an unreadable file instead of promoting an empty artifact', async () => {
    fetchOpts.fileReadOk = false
    mountPanel()
    fireEvent.click(await screen.findByLabelText('Add to artifact library'))
    await waitFor(() => expect(window.alert).toHaveBeenCalledWith('Cannot read file'))
  })

  it('classifies the artifact kind from the extension', async () => {
    mountPanel({ filePath: '/tmp/data.svg', content: '<svg />' })
    fireEvent.click(await screen.findByLabelText('Add to artifact library'))
    await waitFor(() => expect(api.createArtifact).toHaveBeenCalled())
    expect(vi.mocked(api.createArtifact).mock.calls[0][0]).toMatchObject({ kind: 'svg' })
  })

  it('opens the existing artifact instead of offering a second save', async () => {
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [{ slug: 'notes-md', name: 'notes.md' }] } as never)
    mountPanel()
    fireEvent.click(await screen.findByLabelText('Open as artifact'))
    await waitFor(() => expect(screen.getByTestId('pathname')).toHaveTextContent('/artifacts/notes-md'))
  })

  it('snapshots through the ⋯ menu once the file is an artifact', async () => {
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [{ slug: 'notes-md', name: 'notes.md' }] } as never)
    mountPanel()
    await screen.findByLabelText('Open as artifact')
    openPanelMenu()
    fireEvent.click(await screen.findByText('Snapshot version'))
    await waitFor(() => expect(api.updateArtifact).toHaveBeenCalledWith('notes-md', { snapshot: true }))
  })

  it('saves a dirty buffer before snapshotting so the version matches the screen', async () => {
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [{ slug: 'notes-md', name: 'notes.md' }] } as never)
    const onSave = vi.fn(async () => {})
    mountPanel({ content: 'edited body', savedBaseline: 'disk body', onSave })
    await screen.findByLabelText('Open as artifact')
    openPanelMenu()
    fireEvent.click(await screen.findByText('Snapshot version'))
    await waitFor(() => expect(onSave).toHaveBeenCalledWith('/tmp/notes.md', 'edited body'))
    expect(api.updateArtifact).toHaveBeenCalled()
  })
})

describe('MarkdownPanel — knowledge library toggle', () => {
  it('stays hidden while the library is unconfigured', async () => {
    mountPanel()
    await screen.findByLabelText('Add to artifact library')
    expect(screen.queryByLabelText('Add to Knowledge Library')).toBeNull()
  })

  it('registers the file as a local source when clicked', async () => {
    fetchOpts.knowledgeEnabled = true
    mountPanel()
    fireEvent.click(await screen.findByLabelText('Add to Knowledge Library'))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/knowledge/sources', expect.objectContaining({ method: 'POST' })))
    const post = vi.mocked(fetch).mock.calls.find(([, init]) => (init as { method?: string } | undefined)?.method === 'POST')!
    expect(JSON.parse((post[1] as { body: string }).body)).toEqual({
      name: 'notes.md', source_type: 'local_file', uri: '/tmp/notes.md',
    })
  })

  it('surfaces the library error rather than reporting a silent success', async () => {
    fetchOpts.knowledgeEnabled = true
    fetchOpts.knowledgePostStatus = 500
    mountPanel()
    fireEvent.click(await screen.findByLabelText('Add to Knowledge Library'))
    await waitFor(() => expect(window.alert).toHaveBeenCalledWith('library refused'))
  })

  it('renders an inert badge for a file already in the library', async () => {
    fetchOpts.knowledgeEnabled = true
    fetchOpts.knowledgeAdded = true
    mountPanel()
    const badge = await screen.findByLabelText('In Knowledge Library')
    expect(badge.tagName).toBe('SPAN')
    expect(screen.queryByLabelText('Add to Knowledge Library')).toBeNull()
  })
})

describe('MarkdownPanel — authoring an inline comment', () => {
  const BODY = '# Title\n\nalpha beta gamma\n'

  /** Select `word` inside the rendered preview and raise the selection toolbar. */
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

  it('anchors a new comment to the selected text with resolved source coordinates', async () => {
    const onSubmitComments = vi.fn()
    mountPanel({ content: BODY, onSubmitComments })
    fireEvent.click(await selectInPreview('beta'))

    const box = await screen.findByLabelText('Add a comment')
    fireEvent.change(box, { target: { value: 'needs a citation' } })
    fireEvent.click(screen.getByLabelText('Add comment'))

    // The comment lands in the pending list, keyed by its own id.
    await waitFor(() => expect(document.querySelector('[data-comment-id]')).not.toBeNull())
    expect(screen.getByText('needs a citation')).toBeInTheDocument()
    // …and is persisted so it survives a panel close.
    const stored = JSON.parse(localStorage.getItem('mc-comment-drafts') || '{}')
    expect(stored['/tmp/notes.md'][0]).toMatchObject({ anchor: 'beta', text: 'needs a citation', line: 3, column: 7 })
  })

  it('sends every pending comment to the chat and clears the drafts', async () => {
    const onSubmitComments = vi.fn()
    mountPanel({ content: BODY, onSubmitComments })
    fireEvent.click(await selectInPreview('gamma'))
    fireEvent.change(await screen.findByLabelText('Add a comment'), { target: { value: 'rename this' } })
    fireEvent.click(screen.getByLabelText('Add comment'))
    await screen.findByText('rename this')

    fireEvent.click(screen.getByText(/Submit All/))
    await waitFor(() => expect(onSubmitComments).toHaveBeenCalledOnce())
    expect(onSubmitComments.mock.calls[0][0]).toContain('rename this')
    await waitFor(() => expect(screen.queryByText('rename this')).toBeNull())
  })

  it('edits a pending comment in place', async () => {
    mountPanel({ content: BODY, onSubmitComments: vi.fn() })
    fireEvent.click(await selectInPreview('beta'))
    fireEvent.change(await screen.findByLabelText('Add a comment'), { target: { value: 'first draft' } })
    fireEvent.click(screen.getByLabelText('Add comment'))
    await screen.findByText('first draft')

    fireEvent.click(screen.getByLabelText('Edit'))
    const editor = screen.getByDisplayValue('first draft')
    fireEvent.change(editor, { target: { value: 'second draft' } })
    fireEvent.click(screen.getByLabelText('Save'))
    expect(await screen.findByText('second draft')).toBeInTheDocument()
  })

  it('removes a pending comment', async () => {
    mountPanel({ content: BODY, onSubmitComments: vi.fn() })
    fireEvent.click(await selectInPreview('beta'))
    fireEvent.change(await screen.findByLabelText('Add a comment'), { target: { value: 'drop me' } })
    fireEvent.click(screen.getByLabelText('Add comment'))
    await screen.findByText('drop me')

    fireEvent.click(screen.getByLabelText('Remove'))
    await waitFor(() => expect(screen.queryByText('drop me')).toBeNull())
  })

  it('dismisses the popover on Escape without recording a comment', async () => {
    mountPanel({ content: BODY, onSubmitComments: vi.fn() })
    fireEvent.click(await selectInPreview('beta'))
    await screen.findByLabelText('Add a comment')
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByLabelText('Add a comment')).toBeNull())
    expect(document.querySelector('[data-comment-id]')).toBeNull()
  })

  it('offers Copy only, with no Comment action, when the host cannot receive comments', async () => {
    mountPanel({ content: BODY })
    const para = await screen.findByText(/alpha beta gamma/)
    const textNode = para.firstChild as Text
    const range = document.createRange()
    range.setStart(textNode, 0)
    range.setEnd(textNode, 5)
    const sel = window.getSelection()!
    sel.removeAllRanges()
    sel.addRange(range)
    fireEvent.mouseUp(document)
    expect(await screen.findByRole('button', { name: 'Copy' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Comment' })).toBeNull()
  })
})

describe('MarkdownPanel — source-mode view options', () => {
  it('persists each editor view toggle so it survives a remount', () => {
    const { unmount } = mountPanel()
    fireEvent.click(screen.getByText('View Source'))
    const wrap = screen.getByTitle('Toggle word wrap')
    const complete = screen.getByTitle('Toggle autocomplete')
    const nums = screen.getByTitle('Toggle line numbers')
    expect(wrap).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(wrap)
    fireEvent.click(complete)
    fireEvent.click(nums)
    expect(wrap).toHaveAttribute('aria-pressed', 'false')
    expect(complete).toHaveAttribute('aria-pressed', 'false')
    expect(nums).toHaveAttribute('aria-pressed', 'false')
    unmount()

    mountPanel()
    fireEvent.click(screen.getByText('View Source'))
    expect(screen.getByTitle('Toggle word wrap')).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByTitle('Toggle line numbers')).toHaveAttribute('aria-pressed', 'false')
  })

  it('takes the options row out of the tab order while in preview', () => {
    mountPanel()
    // The row stays mounted (grid-rows animation) but must not be reachable.
    expect(screen.getByTitle('Toggle word wrap')).toHaveAttribute('tabindex', '-1')
    fireEvent.click(screen.getByText('View Source'))
    expect(screen.getByTitle('Toggle word wrap')).toHaveAttribute('tabindex', '0')
  })

  it('returns to the preview from source mode via the same toggle', () => {
    mountPanel()
    fireEvent.click(screen.getByText('View Source'))
    expect(screen.getByText('View Preview')).toBeInTheDocument()
    fireEvent.click(screen.getByText('View Preview'))
    expect(screen.getByText('View Source')).toBeInTheDocument()
  })

  it('hides the source/preview toggle for a file whose only renderer is a viewer', async () => {
    mountPanel({ filePath: '/tmp/diagram.png', content: 'iVBORw0KGgo=' })
    await waitFor(() => expect(document.querySelector('img')).not.toBeNull())
    expect(screen.queryByText('View Source')).toBeNull()
    expect(screen.queryByLabelText('Toggle diff view')).toBeNull()
  })
})

describe('MarkdownPanel — comment hint banner', () => {
  it('invites the reader to select text, once, and remembers the dismissal', async () => {
    mountPanel({ onSubmitComments: vi.fn() })
    fireEvent.click(await screen.findByText('Got it'))
    await waitFor(() => expect(screen.queryByText('Got it')).toBeNull())
    expect(localStorage.getItem('kirocrew:comment-hint-dismissed')).toBe('1')
  })

  it('stays away when the panel has nowhere to submit comments', () => {
    mountPanel()
    expect(screen.queryByText('Got it')).toBeNull()
  })
})
