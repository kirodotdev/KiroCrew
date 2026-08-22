/**
 * Viewer components for the Files app: type routing targets (PDF/HTML/CSV/
 * Office), the positioned slide canvas, markdown auto-save, find-in-document
 * range collection, and per-file scroll memory.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { fileExplorerApi } from '../apps/file-explorer/api'
import FileViewer from '../apps/file-explorer/FileViewer'
import { FindBar, findRanges, useFindInDocument } from '../apps/file-explorer/findInDocument'
import { useMarkdownEditor } from '../apps/file-explorer/MarkdownEditor'
import SlideCanvas, { luminance } from '../apps/file-explorer/SlideCanvas'
import { useScrollMemory } from '../apps/file-explorer/useScrollMemory'
import {
  BinaryFallback, DelimitedViewer, HtmlViewer, ImageViewer, MediaViewer, OfficeViewer,
  PdfViewer, extractErrorText, parseDelimited,
} from '../apps/file-explorer/viewers'
import { SCROLL_STORAGE_KEY } from '../apps/file-explorer/constants'
import type { PptxSlide } from '../apps/file-explorer/types'
import { useRef } from 'react'

function withQueryClient(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={client}>{ui}</QueryClientProvider>
}

beforeEach(() => {
  vi.stubGlobal('URL', Object.assign(URL, {
    createObjectURL: vi.fn(() => 'blob:zzz'),
    revokeObjectURL: vi.fn(),
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('parseDelimited', () => {
  it('handles quoted fields, escaped quotes, and CRLF', () => {
    const rows = parseDelimited('a,"b,1","say ""hi"""\r\nc,d,e\n', ',')
    expect(rows).toEqual([['a', 'b,1', 'say "hi"'], ['c', 'd', 'e']])
  })

  it('keeps a trailing unterminated row', () => {
    expect(parseDelimited('x\ty', '\t')).toEqual([['x', 'y']])
  })
})

describe('DelimitedViewer', () => {
  it('renders a table with a header row and toggles to source', () => {
    render(<DelimitedViewer content={'name,qty\nwidget,42\n'} delim="," />)
    expect(screen.getByRole('columnheader', { name: 'name' })).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: 'widget' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Source' }))
    expect(screen.getByText(/widget,42/)).toBeInTheDocument()
  })
})

describe('HtmlViewer', () => {
  it('shows a sandboxed preview iframe and can flip to source', () => {
    const { container } = render(<HtmlViewer content="<h1>zzz page</h1>" />)
    const iframe = container.querySelector('iframe')
    expect(iframe).toBeTruthy()
    expect(iframe!.getAttribute('sandbox')).toBe('')  // fully inert: untrusted content
    expect(iframe!.getAttribute('src')).toBe('blob:zzz')
    fireEvent.click(screen.getByRole('button', { name: 'Source' }))
    expect(screen.getByText(/zzz page/)).toBeInTheDocument()
  })
})

describe('PdfViewer', () => {
  it('frames the raw-bytes URL', () => {
    const { container } = render(<PdfViewer path="/zzz/report.pdf" />)
    const iframe = container.querySelector('iframe')
    expect(iframe!.getAttribute('src')).toBe(fileExplorerApi.rawUrl('/zzz/report.pdf'))
  })
})

describe('SlideCanvas', () => {
  const slide: PptxSlide = {
    n: 1,
    bg: '#161D26',
    lines: ['Big Title'],
    shapes: [
      {
        kind: 'text', x: 10, y: 10, w: 50, h: 20,
        paras: [{ algn: 'ctr', lvl: 0, bullet: false, runs: [{ t: 'Big Title', b: true, sz: 44, c: '#FF0000' }] }],
      },
      { kind: 'image', member: 'ppt/media/image1.png', x: 5, y: 50, w: 25, h: 25 },
    ],
  }

  it('positions shapes, applies run colour/weight, and streams images', () => {
    render(<SlideCanvas slide={slide} path="/zzz/deck.pptx" widthPt={720} ratio={4 / 3} />)
    const canvas = screen.getByTestId('fe-slide-canvas')
    expect(canvas.style.background).toBe('#161D26')
    const run = screen.getByText('Big Title')
    expect(run.style.color).toBe('#FF0000')
    expect(run.style.fontWeight).toBe('600')
    const box = run.closest('div[style*="position: absolute"]') as HTMLElement
    expect(box.style.left).toBe('10%')
    const img = canvas.querySelector('img')!
    expect(img.getAttribute('src')).toBe(fileExplorerApi.extractMemberUrl('/zzz/deck.pptx', 'ppt/media/image1.png'))
  })

  it('auto-contrasts default text against the background luminance', () => {
    expect(luminance('#161D26')).toBeLessThan(0.45)
    expect(luminance('#FFFFFF')).toBeGreaterThan(0.9)
    const noColour: PptxSlide = {
      n: 2, bg: '#111111', lines: [],
      shapes: [{ kind: 'text', paras: [{ algn: 'l', lvl: 0, bullet: false, runs: [{ t: 'plain' }] }] }],
    }
    render(<SlideCanvas slide={noColour} path="/zzz/deck.pptx" widthPt={720} ratio={16 / 9} />)
    expect(screen.getByText('plain').style.color).toBe('#f0f0f0')
  })
})

describe('OfficeViewer', () => {
  it('renders xlsx sheets from /extract as a grid', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        kind: 'xlsx',
        sheets: [{ name: 'Data', rows: [['col', 'n'], ['flumplenook', '84']], truncated: false }],
        path: '/zzz/f.xlsx', size: 1, mtime: 1,
      }),
    }) as unknown as Response))
    render(withQueryClient(<OfficeViewer path="/zzz/f.xlsx" />))
    await waitFor(() => expect(screen.getByRole('cell', { name: 'flumplenook' })).toBeInTheDocument())
  })

  it('surfaces extraction failures instead of an empty pane', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false, status: 415, json: async () => ({}),
      text: async () => 'zzz not extractable',
    }) as unknown as Response))
    render(withQueryClient(<OfficeViewer path="/zzz/f.docx" />))
    await waitFor(() => expect(screen.getByText('Could not extract this document')).toBeInTheDocument(), { timeout: 5000 })
  })
})

describe('useMarkdownEditor', () => {
  function Harness({ path }: { path: string }) {
    const editor = useMarkdownEditor(path)
    return (
      <div>
        <span data-testid="status">{editor.status}</span>
        <span data-testid="warning">{String(editor.statusIsWarning)}</span>
        <button onClick={() => editor.start('# original', 1000)}>start</button>
        <button onClick={() => editor.setBuffer('# edited')}>type</button>
        {editor.editing && <textarea value={editor.buffer ?? ''} readOnly aria-label="buf" />}
      </div>
    )
  }

  it('auto-saves after the debounce and reports Saved', async () => {
    vi.useFakeTimers()
    const write = vi.spyOn(fileExplorerApi, 'write').mockResolvedValue({ ok: true, size: 8, mtime: 2000 })
    render(<Harness path="/zzz/notes.md" />)
    fireEvent.click(screen.getByText('start'))
    fireEvent.click(screen.getByText('type'))
    await act(async () => { await vi.advanceTimersByTimeAsync(1300) })
    expect(write).toHaveBeenCalledWith('/zzz/notes.md', '# edited', 1000, undefined)
    expect(screen.getByTestId('status').textContent).toBe('Saved')
    expect(screen.getByTestId('warning').textContent).toBe('false')
    vi.useRealTimers()
  })

  it('shows the conflict warning on a 409 without clobbering state', async () => {
    vi.useFakeTimers()
    const { FileExplorerApiError } = await import('../apps/file-explorer/api')
    vi.spyOn(fileExplorerApi, 'write').mockRejectedValue(new FileExplorerApiError('changed', 409))
    render(<Harness path="/zzz/notes.md" />)
    fireEvent.click(screen.getByText('start'))
    fireEvent.click(screen.getByText('type'))
    await act(async () => { await vi.advanceTimersByTimeAsync(1300) })
    expect(screen.getByTestId('status').textContent).toContain('File changed on disk')
    expect(screen.getByTestId('warning').textContent).toBe('true')
    vi.useRealTimers()
  })
})

describe('findRanges', () => {
  it('collects case-insensitive match ranges across text nodes', () => {
    const root = document.createElement('div')
    for (const text of ['Alpha beta', 'BETA gamma beta']) {
      const p = document.createElement('p')
      p.textContent = text
      root.appendChild(p)
    }
    const ranges = findRanges(root, 'beta')
    expect(ranges).toHaveLength(3)
    expect(ranges[0].toString().toLowerCase()).toBe('beta')
  })

  it('returns nothing without a query or root', () => {
    expect(findRanges(null, 'x')).toEqual([])
    expect(findRanges(document.createElement('div'), '')).toEqual([])
  })
})

describe('useScrollMemory', () => {
  function Harness({ path }: { path: string }) {
    const ref = useRef<HTMLDivElement | null>(null)
    const onScroll = useScrollMemory(ref, path, true)
    return <div data-testid="body" ref={ref} onScroll={onScroll} style={{ overflow: 'auto', height: 50 }} />
  }

  it('persists the offset (debounced) and restores it for the same file', async () => {
    vi.useFakeTimers()
    const { unmount } = render(<Harness path="/zzz/long.md" />)
    const body = screen.getByTestId('body')
    Object.defineProperty(body, 'scrollTop', { value: 480, writable: true })
    fireEvent.scroll(body)
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    expect(JSON.parse(localStorage.getItem(SCROLL_STORAGE_KEY)!)['/zzz/long.md']).toBe(480)
    unmount()

    render(<Harness path="/zzz/long.md" />)
    const restored = screen.getByTestId('body')
    Object.defineProperty(restored, 'scrollTop', { value: 0, writable: true })
    await act(async () => { await vi.advanceTimersByTimeAsync(300) })
    expect(restored.scrollTop).toBe(480)
    vi.useRealTimers()
  })
})

describe('useFindInDocument + FindBar', () => {
  const harness = () => {
    const registry = new Map<string, unknown>()
    vi.stubGlobal('CSS', { highlights: registry })
    vi.stubGlobal('Highlight', class { ranges: Range[]; constructor(...r: Range[]) { this.ranges = r } })
    function Harness() {
      const bodyRef = useRef<HTMLDivElement | null>(null)
      const find = useFindInDocument(bodyRef, 'k1', true)
      return (
        <div>
          <button data-testid="open" onClick={() => find.setOpen(true)}>o</button>
          <FindBar find={find} fileName="notes.md" />
          <div ref={bodyRef}><p>alpha beta alpha gamma ALPHA</p></div>
        </div>
      )
    }
    return { registry, Harness }
  }

  it('debounces, counts matches, paints highlights, and wraps on jump', async () => {
    vi.useFakeTimers()
    const { registry, Harness } = harness()
    render(<Harness />)
    fireEvent.click(screen.getByTestId('open'))
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: 'alpha' } })
    await act(async () => { vi.advanceTimersByTime(200) })
    expect(screen.getByTestId('fe-find-count').textContent).toBe('1 / 3')
    expect(registry.has('kc-fe-find')).toBe(true)
    expect(registry.has('kc-fe-find-active')).toBe(true)
    // Enter advances, Shift+Enter goes back past 0 and wraps to the end
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(screen.getByTestId('fe-find-count').textContent).toBe('2 / 3')
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
    expect(screen.getByTestId('fe-find-count').textContent).toBe('3 / 3')
    // next/previous buttons drive the same jump
    fireEvent.click(screen.getByRole('button', { name: 'Next match' }))
    expect(screen.getByTestId('fe-find-count').textContent).toBe('1 / 3')
    fireEvent.click(screen.getByRole('button', { name: 'Previous match' }))
    expect(screen.getByTestId('fe-find-count').textContent).toBe('3 / 3')
    // Escape closes and clears
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(screen.queryByRole('textbox')).toBeNull()
    vi.useRealTimers()
  })

  it('clears the count when the query empties and closes via the X button', async () => {
    vi.useFakeTimers()
    const { Harness } = harness()
    render(<Harness />)
    fireEvent.click(screen.getByTestId('open'))
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: 'beta' } })
    await act(async () => { vi.advanceTimersByTime(200) })
    expect(screen.getByTestId('fe-find-count').textContent).toBe('1 / 1')
    fireEvent.change(input, { target: { value: '' } })
    await act(async () => { vi.advanceTimersByTime(200) })
    expect(screen.getByTestId('fe-find-count').textContent).toBe('')
    fireEvent.click(screen.getByRole('button', { name: 'Close find' }))
    expect(screen.queryByRole('textbox')).toBeNull()
    vi.useRealTimers()
  })
})

describe('viewers: media, image, binary fallback', () => {
  it('MediaViewer renders a video element for video and audio + download for audio', () => {
    const { container, rerender } = render(<MediaViewer path="/tmp/clip.mp4" kind="video" />)
    expect(container.querySelector('video')?.getAttribute('src')).toContain('clip.mp4')
    rerender(<MediaViewer path="/tmp/song.mp3" kind="audio" />)
    expect(container.querySelector('audio')?.getAttribute('src')).toContain('song.mp3')
    expect(screen.getByRole('link', { name: /Download/i })).toBeInTheDocument()
  })

  it('ImageViewer streams from the raw endpoint', () => {
    render(<ImageViewer path="/tmp/pic.png" />)
    expect(screen.getByRole('img', { name: 'pic.png' }).getAttribute('src')).toContain('/raw?')
  })

  it('BinaryFallback shows size, mime, and a download affordance', () => {
    render(<BinaryFallback path="/tmp/blob.bin" fileMeta={{ size: 2048, mime: 'application/octet-stream' } as never} />)
    expect(screen.getByTestId('empty-state-subtitle').textContent).toMatch(/octet-stream/)
    expect(screen.getByRole('link', { name: /Download/i })).toBeInTheDocument()
  })
})

describe('OfficeViewer: docx, xlsx sheet toggle, pptx text fallback', () => {
  it('renders docx headings, paragraphs, and tables in order', async () => {
    vi.spyOn(fileExplorerApi, 'extract').mockResolvedValue({
      kind: 'docx',
      blocks: [
        { type: 'h2', text: 'Findings' },
        { type: 'p', text: 'body text here' },
        { type: 'table', rows: [['K', 'V'], ['a', '1']] },
      ],
    } as never)
    render(withQueryClient(<OfficeViewer path="/tmp/r.docx" />))
    await waitFor(() => expect(screen.getByText('Findings')).toBeInTheDocument())
    expect(screen.getByText('body text here')).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'K' })).toBeInTheDocument()
  })

  it('renders a loading notice before extraction resolves', async () => {
    let resolve!: (v: unknown) => void
    vi.spyOn(fileExplorerApi, 'extract').mockReturnValue(new Promise((r) => { resolve = r }) as never)
    render(withQueryClient(<OfficeViewer path="/tmp/slow.docx" />))
    expect(screen.getByText(/Extracting/i)).toBeInTheDocument()
    resolve({ kind: 'docx', blocks: [] })
    await waitFor(() => expect(screen.queryByText(/Extracting/i)).toBeNull())
  })

  it('switches xlsx sheets through the toggle bar and handles empty workbooks', async () => {
    vi.spyOn(fileExplorerApi, 'extract').mockResolvedValue({
      kind: 'xlsx',
      sheets: [
        { name: 'Alpha', rows: [['a1']] },
        { name: 'Beta', rows: [['b1']], truncated: true },
      ],
    } as never)
    render(withQueryClient(<OfficeViewer path="/tmp/w.xlsx" />))
    await waitFor(() => expect(screen.getByRole('columnheader', { name: 'a1' })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Beta' }))
    expect(screen.getByRole('columnheader', { name: 'b1' })).toBeInTheDocument()
    expect(screen.getByText(/Showing first/i)).toBeInTheDocument()
  })

  it('renders pptx slides as plain lines when no shapes were extracted', async () => {
    vi.spyOn(fileExplorerApi, 'extract').mockResolvedValue({
      kind: 'pptx',
      slides: [{ n: 1, bg: null, shapes: [], lines: ['Title line', 'Body line'] }],
    } as never)
    render(withQueryClient(<OfficeViewer path="/tmp/d.pptx" />))
    await waitFor(() => expect(screen.getByText('Title line')).toBeInTheDocument())
    expect(screen.getByText('Body line')).toBeInTheDocument()
    expect(screen.queryByTestId('fe-slide-canvas')).toBeNull()
  })
})

describe('SlideCanvas: image and table shapes', () => {
  it('streams positioned images and renders table shapes', () => {
    const slide: PptxSlide = {
      n: 1,
      bg: '#ffffff',
      lines: [],
      shapes: [
        { kind: 'image', member: 'ppt/media/image1.png', x: 10, y: 10, w: 30, h: 30 },
        { kind: 'table', rows: [['H', 'I'], ['1', '2']], x: 50, y: 50, w: 40, h: 20 },
        { kind: 'image', member: '' } as never, // no member -> falls through to text branch
      ],
    }
    const { container } = render(<SlideCanvas slide={slide} path="/tmp/d.pptx" widthPt={960} ratio={16 / 9} />)
    const img = container.querySelector('img')
    expect(img?.getAttribute('src')).toContain('member=ppt%2Fmedia%2Fimage1.png')
    expect(img?.style.position).toBe('absolute')
    expect(screen.getByText('H')).toBeInTheDocument()
    expect(screen.getByText('2')).toBeInTheDocument()
  })

  it('luminance falls back to bright for malformed colours', () => {
    expect(luminance('#zzzzzz')).toBeGreaterThanOrEqual(0)
    expect(luminance(null)).toBe(1)
  })
})

describe('FileViewer shell', () => {
  const meta = { size: 12, mtime: 1723800000, mime: 'text/markdown', binary: false, encoding: 'utf8' }
  const renderViewer = (props: Partial<React.ComponentProps<typeof FileViewer>> = {}) =>
    render(withQueryClient(
      <FileViewer
        filePath="/tmp/notes.md"
        fileMeta={meta as never}
        content="hello **world**"
        loading={false}
        error={null}
        onReload={vi.fn()}
        onDownload={vi.fn()}
        {...props}
      />,
    ))

  it('shows the empty state without a file and the skeleton while loading', () => {
    renderViewer({ filePath: null })
    expect(screen.getByText(/Select a file/i)).toBeInTheDocument()
    renderViewer({ loading: true })
    renderViewer({ error: 'boom', loading: false })
    expect(screen.getByText('boom')).toBeInTheDocument()
  })

  it('renders the toolbar with one direct action and the overflow', () => {
    renderViewer()
    expect(screen.getByText('notes.md')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Copy path' }))
    // Markdown: the direct action is the edit toggle; find + reload live in ⋯.
    expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Find in document/ })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Reload' })).toBeNull()
    expect(screen.getByRole('button', { name: 'More options' })).toBeInTheDocument()
  })

  it('non-markdown files keep find-in-document as the direct action', () => {
    renderViewer({ filePath: '/tmp/notes.txt' })
    fireEvent.click(screen.getByRole('button', { name: /Find in document/ }))
    expect(screen.getByRole('textbox')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
  })

  it('hides the edit toggle for truncated markdown (data-loss guard)', () => {
    renderViewer({ fileMeta: { ...meta, truncated: true } as never })
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
    // find falls back to being the direct action
    expect(screen.getByRole('button', { name: /Find in document/ })).toBeInTheDocument()
  })

  it('opens find with ctrl/cmd+F through the capture-phase handler', () => {
    renderViewer()
    fireEvent.keyDown(document, { key: 'f', ctrlKey: true })
    expect(screen.getByRole('textbox')).toBeInTheDocument()
  })

  it('flags sensitive paths with the screen-share warning', () => {
    renderViewer({ filePath: '/tmp/.env' })
    expect(screen.getByText(/avoid sharing your screen/i)).toBeInTheDocument()
  })

  it('routes extensions: image, pdf, video, audio, office, html, csv, binary', () => {
    const cases: Array<[string, Partial<typeof meta>, (c: HTMLElement) => void]> = [
      ['/tmp/a.png', {}, (c) => expect(c.querySelector('img')).toBeTruthy()],
      ['/tmp/a.pdf', {}, (c) => expect(c.querySelector('iframe')).toBeTruthy()],
      ['/tmp/a.mp4', {}, (c) => expect(c.querySelector('video')).toBeTruthy()],
      ['/tmp/a.mp3', {}, (c) => expect(c.querySelector('audio')).toBeTruthy()],
      ['/tmp/a.html', {}, (c) => expect(c.querySelector('iframe')).toBeTruthy()],
      ['/tmp/a.csv', {}, (c) => expect(c.querySelector('table')).toBeTruthy()],
      ['/tmp/a.bin', { binary: true, encoding: 'none' }, () =>
        expect(screen.getByText(/Binary file/i)).toBeInTheDocument()],
    ]
    for (const [p, m, check] of cases) {
      const { container, unmount } = render(withQueryClient(
        <FileViewer filePath={p} fileMeta={{ ...meta, ...m } as never} content="x,y\n1,2" loading={false} error={null} onReload={vi.fn()} onDownload={vi.fn()} />,
      ))
      check(container)
      unmount()
    }
  })

  it('enters and leaves markdown edit mode, saving through the API', async () => {
    const write = vi.spyOn(fileExplorerApi, 'write').mockResolvedValue({ mtime: 2 } as never)
    renderViewer()
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    const ta = await screen.findByRole('textbox')
    fireEvent.change(ta, { target: { value: 'edited content' } })
    fireEvent.click(screen.getByRole('button', { name: /Done — save and view/ }))
    await waitFor(() => expect(write).toHaveBeenCalledWith('/tmp/notes.md', 'edited content', 1723800000, undefined))
    await waitFor(() => expect(screen.queryByRole('textbox')).toBeNull())
  })
})

describe('useMarkdownEditor flush-on-switch', () => {
  it('fires a last write for a dirty buffer when the path changes', async () => {
    const write = vi.spyOn(fileExplorerApi, 'write').mockResolvedValue({ mtime: 9 } as never)
    function Host({ path }: { path: string }) {
      const ed = useMarkdownEditor(path)
      return (
        <div>
          <button data-testid="start" onClick={() => ed.start('base', 1)}>s</button>
          <button data-testid="type" onClick={() => ed.setBuffer('dirty')}>t</button>
        </div>
      )
    }
    const { rerender } = render(<Host path="/tmp/a.md" />)
    fireEvent.click(screen.getByTestId('start'))
    fireEvent.click(screen.getByTestId('type'))
    rerender(<Host path="/tmp/b.md" />)
    await waitFor(() => expect(write).toHaveBeenCalledWith('/tmp/a.md', 'dirty', 1, undefined))
  })

  it('finish() reports failure and stays in edit mode on a write error', async () => {
    vi.spyOn(fileExplorerApi, 'write').mockRejectedValue(new Error('disk full'))
    let result: boolean | undefined
    function Host() {
      const ed = useMarkdownEditor('/tmp/x.md')
      return (
        <div>
          <button data-testid="start" onClick={() => ed.start('base', 1)}>s</button>
          <button data-testid="type" onClick={() => ed.setBuffer('changed')}>t</button>
          <button data-testid="finish" onClick={async () => { result = await ed.finish() }}>f</button>
          <span data-testid="status">{ed.status}</span>
        </div>
      )
    }
    render(<Host />)
    fireEvent.click(screen.getByTestId('start'))
    fireEvent.click(screen.getByTestId('type'))
    fireEvent.click(screen.getByTestId('finish'))
    await waitFor(() => expect(result).toBe(false))
    expect(screen.getByTestId('status').textContent).toMatch(/Save failed/i)
  })
})

describe('round 7: review-fix behaviours', () => {
  it('a failed switch-time flush parks the buffer and the next edit recovers it', async () => {
    vi.spyOn(fileExplorerApi, 'write').mockRejectedValue(new Error('network down'))
    function Host({ path }: { path: string }) {
      const ed = useMarkdownEditor(path)
      return (
        <div>
          <button data-testid="start" onClick={() => ed.start('base', 1)}>s</button>
          <button data-testid="type" onClick={() => ed.setBuffer('typed-but-lost?')}>t</button>
          <span data-testid="buffer">{ed.buffer ?? ''}</span>
          <span data-testid="status">{ed.status}</span>
        </div>
      )
    }
    const { rerender } = render(<Host path="/tmp/r.md" />)
    fireEvent.click(screen.getByTestId('start'))
    fireEvent.click(screen.getByTestId('type'))
    rerender(<Host path="/tmp/other.md" />)
    await waitFor(() =>
      expect(localStorage.getItem('kc-fe-recovery:/tmp/r.md')).toBe('typed-but-lost?'),
    )
    // Back on the file: start() OFFERS the parked text without writing —
    // the disk may have moved on, and an auto-save would clobber it.
    const write = vi.mocked(fileExplorerApi.write)
    const callsBefore = write.mock.calls.length
    rerender(<Host path="/tmp/r.md" />)
    fireEvent.click(screen.getByTestId('start'))
    expect(screen.getByTestId('buffer').textContent).toBe('typed-but-lost?')
    expect(screen.getByTestId('status').textContent).toMatch(/Recovered/i)
    // No write fires until the user actually edits; the stash survives
    // so the text cannot be lost twice.
    await new Promise((r) => setTimeout(r, 50))
    expect(write.mock.calls.length).toBe(callsBefore)
    expect(localStorage.getItem('kc-fe-recovery:/tmp/r.md')).toBe('typed-but-lost?')
  })

  it('extractErrorText unwraps the JSON error envelope and passes plain text through', () => {
    expect(extractErrorText(new Error('{"error": "file too large to extract"}')))
      .toBe('file too large to extract')
    expect(extractErrorText(new Error('plain message'))).toBe('plain message')
    expect(extractErrorText(null)).toBe('')
  })

  it('a shape-less slide keeps its text fallback even when other slides have shapes', async () => {
    vi.spyOn(fileExplorerApi, 'extract').mockResolvedValue({
      kind: 'pptx',
      slides: [
        { n: 1, bg: null, shapes: [{ kind: 'text', paras: [{ algn: 'l', lvl: 0, bullet: false, runs: [{ t: 'shaped' }] }] }], lines: ['shaped'] },
        { n: 2, bg: null, shapes: [], lines: ['fallback text line'] },
      ],
    } as never)
    render(withQueryClient(<OfficeViewer path="/tmp/mix.pptx" />))
    await waitFor(() => expect(screen.getByText('fallback text line')).toBeInTheDocument())
    expect(screen.getAllByTestId('fe-slide-canvas')).toHaveLength(1)
  })
})


describe('round 9: review-fix behaviours', () => {
  it('DataGrid caps columns so a single-row stream cannot flood the DOM', () => {
    const wide = Array.from({ length: 300 }, (_, i) => `c${i}`).join(',')
    const { container } = render(<DelimitedViewer content={wide} delim="," />)
    // 300 parsed columns render at most the 200-column cap (+ header row).
    expect(container.querySelectorAll('td, th').length).toBeLessThanOrEqual(401)
    expect(container.querySelectorAll('td, th').length).toBeGreaterThan(0)
    const firstRowCells = container.querySelectorAll('tr:first-child > *').length
    expect(firstRowCells).toBeLessThanOrEqual(200)
  })

  it('second ⌘F closes find and lets the folder-search binding receive the key', () => {
    const meta = { size: 10, mtime: 1, binary: false, encoding: 'utf-8', truncated: false }
    render(withQueryClient(
      <FileViewer filePath="/tmp/notes.txt" fileMeta={meta as never} content="hello"
        loading={false} error={null} onReload={vi.fn()} onDownload={vi.fn()} />,
    ))
    fireEvent.keyDown(document, { key: 'f', ctrlKey: true })
    expect(screen.getByRole('textbox')).toBeInTheDocument()
    // Second press: find closes, and the event is NOT default-prevented,
    // so the page-level folder-search shortcut stays reachable.
    const notPrevented = fireEvent.keyDown(document, { key: 'f', ctrlKey: true })
    expect(notPrevented).toBe(true)
    expect(screen.queryByRole('textbox')).toBeNull()
  })
})
