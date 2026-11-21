/**
 * The hover peek on a chat link to a local file (`markdown/filePeek.tsx`):
 * behind the `link_peeks` feature preview, a link the probe confirmed as a
 * file, carrying a line reference, shows the lines around that line once the
 * pointer rests on it or keyboard focus lands on it. The read is the gateway's
 * own `/api/file-read` with the line window from #40480, asked for the lines
 * shown and no more; anything the gateway refuses leaves the link as it is.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { LinkUnfurlCtx } from '../components/markdown/contexts'
import { FILE_PEEK_CLOSE_MS, FILE_PEEK_OPEN_MS, useFilePeek } from '../components/markdown/filePeek'
import {
  __filePeekInFlight, fetchFilePeek, FilePeekBusy, FILE_PEEK_LINES, FILE_PEEK_MAX_IN_FLIGHT, filePeekWindow,
} from '../components/markdown/filePeekRead'
import { queryClient } from '../api/queryClient'
import { cachePreviewFlag, PREVIEW_LINK_PEEKS } from '../utils/previewFlags'
import { __resetPathKindCache } from '../hooks/usePathKind'
import { isKindsRequest, kindsRequestPaths, kindsResponse } from './pathKindStub'

const FILE = '/home/me/repo/src/app.ts'
const TOTAL = 40
/** Line `n` of the stubbed file: markdown and HTML in it must render as text. */
const lineText = (n: number) => `line ${n} **not bold** <b>not html</b>`

const mouse = { pointerType: 'mouse' }
const realFetch = globalThis.fetch

/** The gateway as the peek sees it: the probe confirms FILE, and file-read
 *  answers the window it was asked for (status and headers per `answer`). */
function stubGateway({ status = 200, headers = true, binary = false }: { status?: number; headers?: boolean; binary?: boolean } = {}) {
  const reads: URL[] = []
  globalThis.fetch = vi.fn((url: unknown, init?: RequestInit) => {
    if (isKindsRequest(url, init)) return Promise.resolve(kindsResponse(kindsRequestPaths(init), p => (p === FILE ? 'file' : null)))
    const u = new URL(String(url), 'http://dashboard.test')
    if (u.pathname !== '/api/file-read') return Promise.resolve(new Response('', { status: 404 }))
    reads.push(u)
    if (status !== 200) return Promise.resolve(new Response(JSON.stringify({ error: 'owner_only' }), { status }))
    if (binary) return Promise.resolve(new Response('', { status: 200, headers: { 'X-File-Binary': 'true' } }))
    const start = Number(u.searchParams.get('start') ?? 1)
    const end = Math.min(TOTAL, Number(u.searchParams.get('end') ?? TOTAL))
    const body = Array.from({ length: end - start + 1 }, (_, i) => lineText(start + i)).join('\n') + '\n'
    const h = headers ? { 'X-Line-Start': String(start), 'X-Line-End': String(end), 'X-Line-Total': String(TOTAL) } : {}
    return Promise.resolve(new Response(body, { status: 200, headers: h }))
  }) as unknown as typeof fetch
  return reads
}

/** Rest the mouse on `el` for the peek's pause. */
function rest(el: Element, ms = FILE_PEEK_OPEN_MS) {
  fireEvent.pointerOver(el, mouse)
  act(() => { vi.advanceTimersByTime(ms) })
}
/** Let the probe batch, the read and react-query's notifier (all on timers)
 *  settle; far shorter than the peek's own pause. */
const flush = () => act(async () => { await vi.advanceTimersByTimeAsync(20) })

/** A message with one file link; the open handler is what lets the probe run. */
function renderLink(href = `${FILE}:20`, onFileOpen = vi.fn()) {
  render(<MarkdownRenderer content={`Look at [app.ts](${href}) first.`} onFileOpen={onFileOpen} />)
  return onFileOpen
}

/** One anchor wired as `MdAnchor` wires the hook, for a context the renderer
 *  does not expose: a streaming message. */
function PeekProbe() {
  const peek = useFilePeek({ path: FILE, line: 20 }, 'probe-peek')
  return <><a ref={peek.ref} href={`${FILE}:20`} {...peek.handlers}>app.ts</a>{peek.panel}</>
}

beforeEach(() => {
  vi.useFakeTimers()
  cachePreviewFlag(PREVIEW_LINK_PEEKS, true)
})
afterEach(() => {
  // Unmount first: turning the preview off fires its change event into a mounted link.
  cleanup()
  globalThis.fetch = realFetch
  __resetPathKindCache()
  queryClient.clear()
  cachePreviewFlag(PREVIEW_LINK_PEEKS, false)
  vi.useRealTimers()
})

describe('filePeekWindow', () => {
  it('puts the referenced line a third of the way down and starts at 1 near the top', () => {
    expect(filePeekWindow(20)).toEqual({ start: 16, end: 27 })
    expect(filePeekWindow(2)).toEqual({ start: 1, end: FILE_PEEK_LINES })
    expect(filePeekWindow(1)).toEqual({ start: 1, end: FILE_PEEK_LINES })
  })
})

describe('the read', () => {
  it('refuses a read past the in-flight bound before any request, and frees the bound when reads settle', async () => {
    const pending: Array<(r: Response) => void> = []
    const fetchMock = vi.fn(() => new Promise<Response>(resolve => { pending.push(resolve) }))
    globalThis.fetch = fetchMock as unknown as typeof fetch
    const range = filePeekWindow(20)
    const first = Array.from({ length: FILE_PEEK_MAX_IN_FLIGHT }, (_, i) => fetchFilePeek(`${FILE}${i}`, range))
    expect(__filePeekInFlight()).toBe(FILE_PEEK_MAX_IN_FLIGHT)
    await expect(fetchFilePeek(`${FILE}-one-more`, range)).rejects.toBeInstanceOf(FilePeekBusy)
    expect(fetchMock).toHaveBeenCalledTimes(FILE_PEEK_MAX_IN_FLIGHT)
    for (const resolve of pending) resolve(new Response('a\nb\n', { status: 200, headers: { 'X-Line-Start': '16' } }))
    await Promise.all(first)
    expect(__filePeekInFlight()).toBe(0)
  })

  it('shows nothing for a gateway that answers the whole file without the window headers', async () => {
    globalThis.fetch = vi.fn(() => Promise.resolve(new Response('whole file\n', { status: 200 }))) as unknown as typeof fetch
    expect(await fetchFilePeek(FILE, filePeekWindow(20))).toEqual({ ok: false, lines: [], start: 0 })
  })
})

describe('file peek in a message', () => {
  it('opens after the pause with the lines around the reference as plain text, the referenced line tinted, and the path to open', async () => {
    const reads = stubGateway()
    renderLink()
    await flush()
    const link = screen.getByRole('link', { name: 'app.ts' })
    expect(reads).toHaveLength(0)

    // Short of the pause nothing opens and nothing is read: a pointer crossing
    // the link costs no request.
    rest(link, FILE_PEEK_OPEN_MS - 1)
    expect(screen.queryByTestId('file-peek-panel')).toBeNull()
    expect(reads).toHaveLength(0)
    act(() => { vi.advanceTimersByTime(1) })
    await flush()

    // Exactly the lines shown were asked for: 12 around line 20.
    expect(reads).toHaveLength(1)
    expect(reads[0].searchParams.get('path')).toBe(FILE)
    expect(reads[0].searchParams.get('start')).toBe('16')
    expect(reads[0].searchParams.get('end')).toBe('27')

    const panel = screen.getByTestId('file-peek-panel')
    expect(screen.getByTestId('file-peek-title')).toHaveTextContent('app.ts')
    const pre = screen.getByTestId('file-peek-lines')
    expect(pre.tagName).toBe('PRE')
    expect(pre).toHaveTextContent('line 16')
    expect(pre).toHaveTextContent('line 27')
    expect(pre).not.toHaveTextContent('line 28')
    // Plain text: the markdown and HTML in the file are characters, not markup.
    expect(pre).toHaveTextContent('**not bold** <b>not html</b>')
    expect(pre.querySelector('strong, b')).toBeNull()
    const marked = panel.querySelectorAll('[data-peek-line="ref"]')
    expect(marked).toHaveLength(1)
    expect(marked[0]).toHaveTextContent('line 20')
    expect(screen.getByTestId('file-peek-path')).toHaveTextContent(FILE)
    expect(panel).toHaveTextContent('Click to open')
    // The body is content the click opens, not a description of the link.
    expect(link).not.toHaveAttribute('aria-describedby')
    expect(link).not.toHaveAttribute('title')
  })

  it('tints the whole range of a line-range reference', async () => {
    stubGateway()
    renderLink(`${FILE}:20-22`)
    await flush()
    rest(screen.getByRole('link', { name: 'app.ts' }))
    await flush()
    const marked = screen.getByTestId('file-peek-panel').querySelectorAll('[data-peek-line="ref"]')
    expect(Array.from(marked, el => el.textContent)).toEqual([20, 21, 22].map(n => `${n} ${lineText(n)}`))
  })

  it('costs one read for two hovers of the same link, and leaves the click as it was', async () => {
    const reads = stubGateway()
    const onFileOpen = renderLink()
    await flush()
    const link = screen.getByRole('link', { name: 'app.ts' })
    rest(link)
    await flush()
    fireEvent.pointerOut(link, mouse)
    act(() => { vi.advanceTimersByTime(FILE_PEEK_CLOSE_MS) })
    expect(screen.queryByTestId('file-peek-panel')).toBeNull()
    rest(link)
    await flush()
    expect(screen.getByTestId('file-peek-panel')).toBeInTheDocument()
    expect(reads).toHaveLength(1)

    fireEvent.click(link)
    expect(onFileOpen).toHaveBeenCalledWith(FILE, expect.objectContaining({ line: 20 }))
  })

  it('stays while the mouse crosses into the panel and closes after it leaves both', async () => {
    stubGateway()
    renderLink()
    await flush()
    const link = screen.getByRole('link', { name: 'app.ts' })
    rest(link)
    await flush()
    const panel = screen.getByTestId('file-peek-panel')

    fireEvent.pointerOut(link, mouse)
    act(() => { vi.advanceTimersByTime(FILE_PEEK_CLOSE_MS - 1) })
    fireEvent.pointerOver(panel, mouse)
    act(() => { vi.advanceTimersByTime(FILE_PEEK_CLOSE_MS * 2) })
    expect(screen.getByTestId('file-peek-panel')).toBe(panel)

    fireEvent.pointerOut(panel, mouse)
    act(() => { vi.advanceTimersByTime(FILE_PEEK_CLOSE_MS) })
    expect(screen.queryByTestId('file-peek-panel')).toBeNull()
  })

  it('opens at once on keyboard focus, closes on Escape, and never opens on a press', async () => {
    stubGateway()
    renderLink()
    await flush()
    const link = screen.getByRole('link', { name: 'app.ts' })

    act(() => { link.focus() })
    await flush()
    expect(screen.getByTestId('file-peek-panel')).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByTestId('file-peek-panel')).toBeNull()
    act(() => { link.blur() })

    fireEvent.pointerDown(link, mouse)
    act(() => { vi.advanceTimersByTime(FILE_PEEK_OPEN_MS * 2) })
    await flush()
    expect(screen.queryByTestId('file-peek-panel')).toBeNull()
  })

  it('shows nothing, and asks once, when the gateway refuses the read', async () => {
    const reads = stubGateway({ status: 403 })
    renderLink()
    await flush()
    const link = screen.getByRole('link', { name: 'app.ts' })
    rest(link)
    await flush()
    expect(screen.queryByTestId('file-peek-panel')).toBeNull()
    fireEvent.pointerOut(link, mouse)
    act(() => { vi.advanceTimersByTime(FILE_PEEK_CLOSE_MS) })
    rest(link)
    await flush()
    expect(screen.queryByTestId('file-peek-panel')).toBeNull()
    expect(reads).toHaveLength(1)
  })

  it('shows nothing for a binary file and for a gateway without the line window', async () => {
    stubGateway({ binary: true })
    const { unmount } = render(<MarkdownRenderer content={`See [logo](/home/me/repo/src/app.ts:1).`} onFileOpen={vi.fn()} />)
    await flush()
    rest(screen.getByRole('link', { name: 'logo' }))
    await flush()
    expect(screen.queryByTestId('file-peek-panel')).toBeNull()
    unmount()
    queryClient.clear()

    stubGateway({ headers: false })
    renderLink()
    await flush()
    rest(screen.getByRole('link', { name: 'app.ts' }))
    await flush()
    expect(screen.queryByTestId('file-peek-panel')).toBeNull()
  })

  it('gives a file link with no line reference no peek and no read', async () => {
    const reads = stubGateway()
    renderLink(FILE)
    await flush()
    rest(screen.getByRole('link', { name: 'app.ts' }))
    await flush()
    expect(screen.queryByTestId('file-peek-panel')).toBeNull()
    expect(reads).toHaveLength(0)
  })

  it('shows nothing with the preview flag off, while a web link keeps its address bubble', async () => {
    const reads = stubGateway()
    cachePreviewFlag(PREVIEW_LINK_PEEKS, false)
    render(<MarkdownRenderer content={`Read [app.ts](${FILE}:20) and [the docs](https://example.com/docs).`} onFileOpen={vi.fn()} />)
    await flush()
    rest(screen.getByRole('link', { name: 'app.ts' }))
    await flush()
    expect(screen.queryByTestId('file-peek-panel')).toBeNull()
    expect(reads).toHaveLength(0)

    fireEvent.mouseEnter(screen.getByRole('link', { name: 'the docs' }))
    act(() => { vi.advanceTimersByTime(FILE_PEEK_OPEN_MS) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('https://example.com/docs')
  })

  it('shows nothing while the message is still streaming', async () => {
    const reads = stubGateway()
    // The renderer holds a live tail's links back as plain text, so the gate
    // is exercised through the hook under a streaming block's own context.
    render(
      <LinkUnfurlCtx.Provider value={{ enabled: false, live: true }}>
        <PeekProbe />
      </LinkUnfurlCtx.Provider>,
    )
    await flush()
    rest(screen.getByRole('link', { name: 'app.ts' }))
    await flush()
    expect(screen.queryByTestId('file-peek-panel')).toBeNull()
    expect(reads).toHaveLength(0)
  })
})
