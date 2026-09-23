/**
 * The side panel's binary-file fallback, end to end below the panel.
 *
 * Three layers, tested together because each one alone is inert:
 *
 *  1. ROUTING — `/api/file-read` answers `X-File-Binary`, so `openFile` opens
 *     the tab with an EMPTY buffer and `binary: true` rather than reading the
 *     envelope as if it were the file's text.
 *  2. TAB STATE — `usePanelTabs` carries the verdict, and drops it from
 *     persistence alongside the `content` whose absence it explains.
 *  3. CARD — what renders instead of the Pierre editor.
 *
 * Before this, an unknown extension fell through `detectFileType` to `'code'`
 * and the editor showed the whole file decoded with `errors="replace"`: a
 * screenful of U+FFFD that also looked saveable back over the real bytes.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, renderHook, screen, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import { BinaryFileCard } from '../components/FileRenderers'
import { usePanelDocumentActions } from '../hooks/usePanelDocumentActions'
import { usePanelTabs } from '../hooks/usePanelTabs'

function renderWithQuery(ui: ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>)
}

describe('BinaryFileCard', () => {
  it('names the file and offers a download of the real bytes, not the buffer', () => {
    renderWithQuery(<BinaryFileCard filePath="/home/user/data/index.sqlite" />)
    expect(screen.getByTestId('binary-file-card')).toBeInTheDocument()
    expect(screen.getByText('index.sqlite')).toBeInTheDocument()
    const link = screen.getByRole('link', { name: /index\.sqlite/i })
    // /api/file-download, NOT file-read: the card exists precisely because
    // file-read cannot represent these bytes.
    expect(link.getAttribute('href')).toBe(
      '/api/file-download?path=%2Fhome%2Fuser%2Fdata%2Findex.sqlite',
    )
    expect(link.getAttribute('download')).toBe('index.sqlite')
  })

  it('says the file is not text rather than showing an empty editor', () => {
    renderWithQuery(<BinaryFileCard filePath="/tmp/blob.bin" />)
    expect(screen.getByText("This file can't be previewed")).toBeInTheDocument()
    expect(screen.getByText(/nothing to show here/i)).toBeInTheDocument()
    // Remote browser (the test default: no direct-local branding): no Open
    // button renders, so the hint must not name one.
    expect(screen.queryByText(/open it in its app/i)).toBeNull()
    expect(screen.getByText(/download/i, { selector: 'div' })).toBeInTheDocument()
  })

  it('badges an extension-less binary BIN instead of an empty pill', () => {
    // The verdict is a byte sniff, not an extension list, so `coredump` reaches
    // this card with no extension to derive a badge from.
    renderWithQuery(<BinaryFileCard filePath="/var/crash/coredump" />)
    expect(screen.getByText('BIN')).toBeInTheDocument()
    expect(screen.getByText('coredump')).toBeInTheDocument()
  })

  it('splits a Windows path on the backslash so the name is not the whole path', () => {
    renderWithQuery(<BinaryFileCard filePath={'C:\\Users\\zzq\\Downloads\\archive.zip'} />)
    expect(screen.getByText('archive.zip')).toBeInTheDocument()
    expect(screen.getByText('ZIP')).toBeInTheDocument()
  })
})

describe('openFile binary routing', () => {
  const realFetch = globalThis.fetch
  afterEach(() => {
    globalThis.fetch = realFetch
    vi.restoreAllMocks()
  })

  /** Stub /api/file-read; /api/file-diff is prefetched alongside it. */
  function stubRead({ binary, body }: { binary: boolean; body: string }) {
    globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/file-read')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          headers: { get: (h: string) => (h === 'X-File-Binary' && binary ? 'true' : null) },
          text: () => Promise.resolve(body),
        } as unknown as Response)
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({}),
      } as unknown as Response)
    }) as unknown as typeof fetch
  }

  function harness() {
    const openFileSpy = vi.fn()
    const tabsCtl = { openFile: openFileSpy } as unknown as Parameters<
      typeof usePanelDocumentActions
    >[0]['tabsCtl']
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result } = renderHook(() =>
      usePanelDocumentActions({
        tabsCtl,
        slotRef: { current: 'zzq-slot' },
        queryClient,
        showActionError: vi.fn(),
      }),
    )
    return { result, openFileSpy }
  }

  it('opens a binary file with an empty buffer and the binary flag set', async () => {
    // The body is deliberately non-empty: the routing must not read it. Putting
    // the envelope JSON in the buffer would render as the file's own text.
    stubRead({ binary: true, body: '{"binary": true, "size": 4096}' })
    const { result, openFileSpy } = harness()
    await act(async () => {
      await result.current.openFile('/home/user/data/index.sqlite')
    })
    await waitFor(() => expect(openFileSpy).toHaveBeenCalled())
    const [path, content, , opts] = openFileSpy.mock.calls[0]
    expect(path).toBe('/home/user/data/index.sqlite')
    expect(content).toBe('')
    expect(opts.binary).toBe(true)
  })

  it('leaves a text file buffer and flag alone', async () => {
    stubRead({ binary: false, body: 'x = 1\n' })
    const { result, openFileSpy } = harness()
    await act(async () => {
      await result.current.openFile('/home/user/app.py')
    })
    await waitFor(() => expect(openFileSpy).toHaveBeenCalled())
    const [, content, , opts] = openFileSpy.mock.calls[0]
    expect(content).toBe('x = 1\n')
    expect(opts.binary).toBe(false)
  })
})

describe('usePanelTabs binary verdict', () => {
  it('stamps the verdict on the tab and clears it when the file becomes text', () => {
    const { result } = renderHook(() => usePanelTabs('zzq-bucket'))
    act(() => { result.current.openFile('/tmp/blob.bin', '', null, { binary: true }) })
    expect(result.current.tabs.find(t => t.path === '/tmp/blob.bin')?.binary).toBe(true)
    // A re-open of the same path with no verdict must not inherit the old one:
    // `upsert` spreads onto the existing tab, so the key has to be written on
    // every open rather than omitted when absent.
    act(() => { result.current.openFile('/tmp/blob.bin', 'now text', null) })
    const tab = result.current.tabs.find(t => t.path === '/tmp/blob.bin')
    expect(tab?.binary).toBeUndefined()
    expect(tab?.content).toBe('now text')
  })

  it('drops the verdict from persistence, like the content it explains', () => {
    const key = 'mc-panel-tabs:zzq-persist'
    const { result } = renderHook(() => usePanelTabs('zzq-persist'))
    act(() => { result.current.openFile('/tmp/blob.bin', '', null, { binary: true }) })
    const stored = window.localStorage.getItem(key) ?? window.sessionStorage.getItem(key) ?? ''
    // A persisted verdict would outlive the file: the hydration read that
    // refills the buffer is what re-establishes it.
    expect(stored).not.toContain('"binary"')
  })
})
