/**
 * Which file tabs hold a `/api/file-watch` stream (#14236).
 *
 * `SidePanel` keeps every document tab mounted and merely hides the inactive
 * ones, so N open files means N live `MarkdownPanel` instances. Each used to
 * arm its own watch whether or not it was on screen, and every watch is one
 * `EventSource` -- one HTTP/1.1 connection held open for as long as the tab
 * exists. Chromium allows six connections per host, so six idle tabs starved
 * every other request the dashboard makes: uploads, sends, polls. Only the tab
 * the user can see may hold a stream.
 *
 * Parking a hidden tab's watch has a regression hiding in it: nothing tells a
 * hidden tab about a change made while it was parked, so it would show stale
 * content on reactivation -- silently, because its saved baseline still looks
 * current. The tab therefore re-reads its file when it becomes the visible one,
 * through the same disk-read pair the watch uses and under the same guards: a
 * tab with unsaved edits keeps them and does not re-read.
 *
 * `Highlight` / `CSS.highlights` are stubbed BEFORE the dynamic import because
 * MarkdownPanel captures both into module-level constants at load time. Pierre
 * is stubbed because markdown preview never mounts it and the real module
 * pulls in Shiki.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createRef, forwardRef, useImperativeHandle, useState } from 'react'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PierreEditorHandle } from '../pierre'

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
  PierreEditor: forwardRef<PierreEditorHandle, {
    file: { contents: string }
    onChange?: (value: string) => void
    onSave?: () => void
  }>(function PierreEditorStub({ file, onChange, onSave }, ref) {
    useImperativeHandle(ref, () => ({ jumpToLine: () => {}, focus: () => {} }), [])
    // A textarea stands in for Pierre so a test can type: `fireEvent.change`
    // reaches the panel's own onChange the way a keystroke does, and the save
    // button stands in for Cmd+S.
    return (
      <>
        <textarea data-testid="pierre-editor" aria-label="editor" value={file.contents} onChange={e => onChange?.(e.target.value)} />
        <button type="button" data-testid="pierre-save" onClick={() => onSave?.()}>save</button>
      </>
    )
  }),
  PierreCode: ({ file }: { file: { contents: string } }) => (
    <div data-testid="pierre-code" data-value={file.contents} />
  ),
  PierreFilePair: () => <div data-testid="pierre-diff" />,
}))

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn(async () => true) }))

// `fetchFileRead` is the ONE way a panel's read reaches the disk, so it is
// spied on: the structural claim of this module is that every catch-up read is
// a call of it -- never the read that opened the tab, never a cached body.
vi.mock('../utils/fileReadQuery', async importOriginal => {
  const actual = await importOriginal<typeof import('../utils/fileReadQuery')>()
  return { ...actual, fetchFileRead: vi.fn(actual.fetchFileRead) }
})

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
const { fetchFileRead } = await import('../utils/fileReadQuery')
const { copyToClipboard } = await import('../utils/clipboard')
const { i18nT } = await import('../i18n/t')
const { default: MarkdownPanel } = await import('../components/MarkdownPanel')

/** Reads of `path` that went through `fetchFileRead` -- fresh fetches of the
 *  disk, as opposed to `readsOf`, which counts what the network saw. The two
 *  agree exactly when no read was served from anywhere else. */
const freshReadsOf = (path: string) => vi.mocked(fetchFileRead).mock.calls.filter(([p]) => p === path).length

// ── EventSource stub: every instance is recorded, and `close()` is what tells
// a parked stream from a live one. Nothing fires on its own: a stream that
// delivers no event is exactly the case the activation re-read must not depend
// on (a connection the browser is still holding in `blocked` delivers nothing
// either).
class StubEventSource {
  static instances: StubEventSource[] = []
  closed = false
  onopen: (() => void) | null = null
  onmessage: ((ev: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  constructor(readonly url: string) { StubEventSource.instances.push(this) }
  close() { this.closed = true }
}
const openStreams = () => StubEventSource.instances.filter(s => !s.closed)
const watchedPath = (s: StubEventSource) =>
  decodeURIComponent(new URL(s.url, 'http://gateway').searchParams.get('path') ?? '')

// ── fetch router: `/api/file-read` answers from `disk`, keyed by path, and every
// such read is counted per path. A path in `held` does not answer until its
// release runs -- the window in which the user can act on a tab whose catch-up
// read has not landed. A path in `unreadable` answers 404, the way a deleted or
// moved file does; one in `broken` answers 500, a read that really failed.
// Everything else the panel fetches on mount (knowledge config, artifact state)
// gets an inert answer.
const disk = new Map<string, string>()
const fileReads: string[] = []
const held = new Map<string, Array<() => void>>()
const unreadable = new Set<string>()
const broken = new Set<string>()
/** Paths whose reads answer `X-Truncated: true` -- the gateway cut the body. */
const partialReads = new Set<string>()
/** Paths whose reads answer `X-Lossy-Decode: true` -- not valid UTF-8 as written. */
const lossyReads = new Set<string>()
function holdReads(path: string) {
  held.set(path, [])
  return () => { for (const answer of held.get(path) ?? []) answer(); held.delete(path) }
}
function installFetch() {
  vi.stubGlobal('fetch', vi.fn(async (input: unknown) => {
    const url = String(input)
    if (url.startsWith('/api/file-read')) {
      const path = decodeURIComponent(new URL(url, 'http://gateway').searchParams.get('path') ?? '')
      fileReads.push(path)
      // A held read answers with the bytes the file had when the request went
      // out, the way a slow server does; a save made meanwhile is not in them.
      const body = disk.get(path) ?? ''
      const gate = held.get(path)
      if (gate) await new Promise<void>(answer => gate.push(answer))
      if (unreadable.has(path) || broken.has(path)) {
        return { ok: false, status: unreadable.has(path) ? 404 : 500, headers: { get: () => null }, text: async () => '' }
      }
      return {
        ok: true, status: 200, headers: { get: (h: string) => (h === 'X-Truncated' && partialReads.has(path)) || (h === 'X-Lossy-Decode' && lossyReads.has(path)) ? 'true' : null },
        text: async () => body,
      }
    }
    return {
      ok: true, status: 200, headers: { get: () => null },
      json: async () => ({ enabled: false, supported_formats: [] }),
      text: async () => '',
    }
  }))
}
const readsOf = (path: string) => fileReads.filter(p => p === path).length

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter>
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  </MemoryRouter>
)

const TAB_PATHS = ['/tmp/a.md', '/tmp/b.md', '/tmp/c.md', '/tmp/d.md', '/tmp/e.md', '/tmp/f.md']
const bodyOf = (path: string) => `# ${path}\n\nas opened\n`

/**
 * Six file tabs, mounted the way `SidePanel` mounts them: every one live, one
 * visible, `liveWatch` on all of them, `onDiskContent` restamping the tab's
 * buffer and saved baseline the way the side panel's `patchTab` does. A tab in
 * `edited` carries unsaved edits (its buffer differs from its saved baseline).
 */
function SixTabs({ activePath, edited = [], onDiskContent }: {
  activePath: string
  edited?: string[]
  onDiskContent: (path: string, text: string, binary: boolean) => void
}) {
  return (
    <>
      {TAB_PATHS.map(path => (
        <div key={path} style={{ display: path === activePath ? 'block' : 'none' }}>
          <MarkdownPanel
            embedded
            liveWatch
            active={path === activePath}
            filePath={path}
            content={edited.includes(path) ? bodyOf(path) + 'unsaved line\n' : bodyOf(path)}
            savedBaseline={bodyOf(path)}
            onContentChange={() => {}}
            onDiskContent={(text, binary) => onDiskContent(path, text, binary)}
            onSave={async () => {}}
            onClose={() => {}}
          />
        </div>
      ))}
    </>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  qc.clear()
  highlightRegistry.clear()
  localStorage.clear()
  StubEventSource.instances = []
  disk.clear()
  fileReads.length = 0
  held.clear()
  unreadable.clear()
  broken.clear()
  partialReads.clear()
  lossyReads.clear()
  pendingSaves = null
  for (const path of TAB_PATHS) disk.set(path, bodyOf(path))
  vi.stubGlobal('EventSource', StubEventSource)
  installFetch()
  Object.defineProperty(Element.prototype, 'scrollIntoView', {
    configurable: true, writable: true, value: vi.fn(),
  })
  vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [] } as never)
  vi.mocked(api.artifact).mockResolvedValue({ live_dirty: false, pinned: false } as never)
  vi.mocked(api.fileDiff).mockResolvedValue({ diff: '', original: '', status: 'clean' } as never)
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  document.body.style.overflow = ''
})

/** Let every effect and settled promise land. */
const settle = () => new Promise(resolve => setTimeout(resolve, 30))

describe('MarkdownPanel — the file watch follows the visible tab', () => {
  it('holds exactly one stream for six mounted tabs, and it is the visible tab\'s', async () => {
    render(<SixTabs activePath="/tmp/c.md" onDiskContent={vi.fn()} />, { wrapper })

    await waitFor(() => expect(openStreams()).toHaveLength(1))
    await settle()
    // Six live panels, ONE connection: the five hidden tabs never opened a
    // stream at all -- `instances` counts every construction, closed or not.
    expect(StubEventSource.instances).toHaveLength(1)
    expect(watchedPath(openStreams()[0])).toBe('/tmp/c.md')
    // The visible tab reads once on mount, hidden tabs not at all: the buffer a
    // remounted tab is handed may predate a change made while the panel was
    // unmounted, and the stream's connect-time frame does not cover a file
    // emptied meanwhile (`api_file_watch` emits only when content differs from
    // its initial empty string).
    expect(fileReads).toEqual(['/tmp/c.md'])
  })

  it('re-reads a remounted visible tab, so a file emptied while the panel was unmounted is adopted', async () => {
    // The tab store outlives the panel. A remount hands the panel the buffer as
    // it was; if the file was emptied meanwhile nothing else says so -- the
    // re-armed stream never emits an empty first frame -- and the stale buffer
    // would be saved back over the emptied file.
    const onDiskContent = vi.fn()
    const { unmount } = render(<SixTabs activePath="/tmp/c.md" onDiskContent={onDiskContent} />, { wrapper })
    await waitFor(() => expect(readsOf('/tmp/c.md')).toBe(1))
    unmount()

    disk.set('/tmp/c.md', '')
    render(<SixTabs activePath="/tmp/c.md" onDiskContent={onDiskContent} />, { wrapper })
    await waitFor(() => expect(onDiskContent).toHaveBeenCalledWith('/tmp/c.md', '', false))
    await settle()
    expect(readsOf('/tmp/c.md')).toBe(2)
    expect(onDiskContent).toHaveBeenCalledTimes(1)
  })

  it('moves the stream to the newly visible tab and re-reads that file once', async () => {
    const onDiskContent = vi.fn()
    const { rerender } = render(
      <SixTabs activePath="/tmp/a.md" onDiskContent={onDiskContent} />, { wrapper })
    await waitFor(() => expect(openStreams()).toHaveLength(1))
    const first = openStreams()[0]
    expect(watchedPath(first)).toBe('/tmp/a.md')

    // The file behind the hidden tab changes on disk while it holds no stream.
    disk.set('/tmp/d.md', '# /tmp/d.md\n\nrewritten while hidden\n')

    rerender(<SixTabs activePath="/tmp/d.md" onDiskContent={onDiskContent} />)

    // The old tab's stream is closed and the new one is the only stream open.
    await waitFor(() => expect(openStreams()).toHaveLength(1))
    expect(first.closed).toBe(true)
    expect(watchedPath(openStreams()[0])).toBe('/tmp/d.md')

    // The newly visible tab re-reads its file -- once -- and restamps the tab
    // from the disk truth, so the change made while it was parked is on screen.
    await waitFor(() => expect(onDiskContent).toHaveBeenCalledWith(
      '/tmp/d.md', '# /tmp/d.md\n\nrewritten while hidden\n', false))
    await settle()
    expect(readsOf('/tmp/d.md')).toBe(1)
    expect(onDiskContent).toHaveBeenCalledTimes(1)
    // Losing visibility is not a reason to read: the tab that was hidden keeps
    // its one mount read, and no other hidden tab reads at all.
    expect(readsOf('/tmp/a.md')).toBe(1)
    expect(fileReads).toEqual(['/tmp/a.md', '/tmp/d.md'])
  })

  it('does not re-read, and holds no stream, for a tab with unsaved edits', async () => {
    const onDiskContent = vi.fn()
    const { rerender } = render(
      <SixTabs activePath="/tmp/a.md" edited={['/tmp/b.md']} onDiskContent={onDiskContent} />, { wrapper })
    await waitFor(() => expect(openStreams()).toHaveLength(1))

    disk.set('/tmp/b.md', '# /tmp/b.md\n\nrewritten while hidden\n')
    rerender(<SixTabs activePath="/tmp/b.md" edited={['/tmp/b.md']} onDiskContent={onDiskContent} />)
    await settle()

    // The edits are the user's work; a disk push would clobber them. No read,
    // no restamp, and no stream either -- a dirty tab never watches.
    expect(readsOf('/tmp/b.md')).toBe(0)
    expect(onDiskContent).not.toHaveBeenCalled()
    expect(openStreams()).toHaveLength(0)

    // The user undoes the edits while the tab is visible: the buffer is back at
    // its saved baseline, which is the PRE-change revision. Left alone the tab
    // would read as clean and current while showing text the file no longer
    // has, and the next save would write it over the newer file. The catch-up
    // the edits held back runs now.
    rerender(<SixTabs activePath="/tmp/b.md" onDiskContent={onDiskContent} />)
    await waitFor(() => expect(onDiskContent).toHaveBeenCalledWith(
      '/tmp/b.md', '# /tmp/b.md\n\nrewritten while hidden\n', false))
    await settle()
    expect(readsOf('/tmp/b.md')).toBe(1)
    expect(openStreams()).toHaveLength(1)
    expect(watchedPath(openStreams()[0])).toBe('/tmp/b.md')
  })

  it('re-reads on each return to the tab, not only the first', async () => {
    const onDiskContent = vi.fn()
    const { rerender } = render(
      <SixTabs activePath="/tmp/a.md" onDiskContent={onDiskContent} />, { wrapper })
    await waitFor(() => expect(openStreams()).toHaveLength(1))

    rerender(<SixTabs activePath="/tmp/b.md" onDiskContent={onDiskContent} />)
    await waitFor(() => expect(readsOf('/tmp/b.md')).toBe(1))

    // Back to the first tab: it was parked while B was visible, so it reads
    // again (its first read was the mount).
    disk.set('/tmp/a.md', '# /tmp/a.md\n\nchanged during the visit to b\n')
    rerender(<SixTabs activePath="/tmp/a.md" onDiskContent={onDiskContent} />)
    await waitFor(() => expect(onDiskContent).toHaveBeenCalledWith(
      '/tmp/a.md', '# /tmp/a.md\n\nchanged during the visit to b\n', false))
    await settle()
    expect(readsOf('/tmp/a.md')).toBe(2)
    expect(openStreams()).toHaveLength(1)
    expect(watchedPath(openStreams()[0])).toBe('/tmp/a.md')
  })

  it('re-reads a clean tab that sits in the editor, which holds no stream', async () => {
    // A code file opens straight in the editor, so `editing` is true for as long
    // as the tab lives. The stream stays off for an editing tab -- a live push
    // would land under the user's cursor -- but the catch-up read is what keeps
    // a clean editor buffer from going stale while the tab was hidden. Without
    // it the user returns to the old revision, edits it and saves, and the newer
    // file on disk is overwritten.
    const onDiskContent = vi.fn()
    const code = '/tmp/x.ts'
    const asOpened = 'export const x = 1\n'
    const Tabs = ({ activePath }: { activePath: string }) => (
      <>
        <div style={{ display: activePath === '/tmp/a.md' ? 'block' : 'none' }}>
          <MarkdownPanel
            embedded liveWatch active={activePath === '/tmp/a.md'}
            filePath="/tmp/a.md" content={bodyOf('/tmp/a.md')} savedBaseline={bodyOf('/tmp/a.md')}
            onContentChange={() => {}} onDiskContent={(t, b) => onDiskContent('/tmp/a.md', t, b)}
            onSave={async () => {}} onClose={() => {}}
          />
        </div>
        <div style={{ display: activePath === code ? 'block' : 'none' }}>
          <MarkdownPanel
            embedded liveWatch active={activePath === code}
            filePath={code} content={asOpened} savedBaseline={asOpened}
            onContentChange={() => {}} onDiskContent={(t, b) => onDiskContent(code, t, b)}
            onSave={async () => {}} onClose={() => {}}
          />
        </div>
      </>
    )
    disk.set(code, asOpened)
    const { rerender } = render(<Tabs activePath="/tmp/a.md" />, { wrapper })
    await waitFor(() => expect(openStreams()).toHaveLength(1))

    disk.set(code, 'export const x = 2\n')
    rerender(<Tabs activePath={code} />)

    await waitFor(() => expect(onDiskContent).toHaveBeenCalledWith(code, 'export const x = 2\n', false))
    await settle()
    expect(readsOf(code)).toBe(1)
    // Still no stream for the editing tab: the markdown tab's stream closed with
    // its visibility, and the editor tab arms none.
    expect(openStreams()).toHaveLength(0)
  })

  it('re-reads once edits typed before the catch-up read landed are undone', async () => {
    // The tab used to count as caught up the moment its read STARTED. A hidden
    // file changes; the user activates the tab and types before the read lands,
    // so the result is dropped (the edits are the user's work) -- and the tab
    // stayed marked caught up. Undoing the edits then left a clean buffer at the
    // pre-change revision that nothing re-read, and the next save wrote it over
    // the newer file. Only an APPLIED read may mark the tab caught up: a dropped
    // one leaves it due, and it reads the moment it is clean again.
    const onDiskContent = vi.fn()
    const { rerender } = render(
      <SixTabs activePath="/tmp/a.md" onDiskContent={onDiskContent} />, { wrapper })
    await waitFor(() => expect(openStreams()).toHaveLength(1))

    disk.set('/tmp/b.md', '# /tmp/b.md\n\nrewritten while hidden\n')
    const land = holdReads('/tmp/b.md')
    rerender(<SixTabs activePath="/tmp/b.md" onDiskContent={onDiskContent} />)
    await waitFor(() => expect(readsOf('/tmp/b.md')).toBe(1))

    // Typing before the read lands: the buffer is dirty when the result arrives,
    // so it is dropped and nothing is restamped.
    rerender(<SixTabs activePath="/tmp/b.md" edited={['/tmp/b.md']} onDiskContent={onDiskContent} />)
    land()
    await settle()
    expect(onDiskContent).not.toHaveBeenCalled()
    expect(readsOf('/tmp/b.md')).toBe(1)

    // Undo: clean again, and at the PRE-change revision. The disk revision the
    // user never saw lands now, on a second read, instead of the tab sitting
    // there looking current.
    rerender(<SixTabs activePath="/tmp/b.md" onDiskContent={onDiskContent} />)
    await waitFor(() => expect(onDiskContent).toHaveBeenCalledWith(
      '/tmp/b.md', '# /tmp/b.md\n\nrewritten while hidden\n', false))
    await settle()
    expect(readsOf('/tmp/b.md')).toBe(2)
    expect(onDiskContent).toHaveBeenCalledTimes(1)
  })

  it('does not count a read that lands after the tab was hidden again', async () => {
    // Activate B, switch back to A before B's read lands, then let it land: the
    // bytes are the disk truth and are applied, but B is hidden by then and its
    // file may change again before the user returns. Counting that landing as
    // the catch-up would skip the read the next return needs.
    const onDiskContent = vi.fn()
    const { rerender } = render(
      <SixTabs activePath="/tmp/a.md" onDiskContent={onDiskContent} />, { wrapper })
    await waitFor(() => expect(openStreams()).toHaveLength(1))

    disk.set('/tmp/b.md', '# /tmp/b.md\n\nfirst rewrite\n')
    const land = holdReads('/tmp/b.md')
    rerender(<SixTabs activePath="/tmp/b.md" onDiskContent={onDiskContent} />)
    await waitFor(() => expect(readsOf('/tmp/b.md')).toBe(1))
    rerender(<SixTabs activePath="/tmp/a.md" onDiskContent={onDiskContent} />)
    land()
    await waitFor(() => expect(onDiskContent).toHaveBeenCalledWith(
      '/tmp/b.md', '# /tmp/b.md\n\nfirst rewrite\n', false))

    disk.set('/tmp/b.md', '# /tmp/b.md\n\nsecond rewrite\n')
    rerender(<SixTabs activePath="/tmp/b.md" onDiskContent={onDiskContent} />)
    await waitFor(() => expect(onDiskContent).toHaveBeenCalledWith(
      '/tmp/b.md', '# /tmp/b.md\n\nsecond rewrite\n', false))
    expect(readsOf('/tmp/b.md')).toBe(2)
  })

  it('keeps the last copy on screen under a banner for a file that is gone, with no notice', async () => {
    // A 404 is a real answer about the file -- it is not on disk -- and a tab
    // coming back to a moved or deleted file says so, not with a red "Cannot
    // read file" notice on every return, and not by hiding the document: the
    // buffer and its saved baseline keep the last content, which may be the
    // only copy left of a file deleted outside the dashboard, so that copy
    // stays on screen, to read, copy, snapshot, download or save back, under a
    // banner that says what it is. A later read that finds the file again
    // clears the banner.
    const onDiskContent = vi.fn()
    const { rerender } = render(
      <SixTabs activePath="/tmp/a.md" onDiskContent={onDiskContent} />, { wrapper })
    await waitFor(() => expect(openStreams()).toHaveLength(1))

    unreadable.add('/tmp/b.md')
    rerender(<SixTabs activePath="/tmp/b.md" onDiskContent={onDiskContent} />)
    await waitFor(() => expect(screen.getByTestId('markdown-panel-missing-file')).toBeTruthy())
    await settle()
    expect(screen.getByTestId('markdown-panel-missing-file').textContent).toMatch(/file not found on disk.*nothing else holds/i)
    // The document is still rendered: the last copy's heading is on screen.
    expect(screen.getByRole('heading', { name: '/tmp/b.md' })).toBeTruthy()
    expect(onDiskContent).not.toHaveBeenCalled()
    expect(screen.queryByText(/cannot read file/i)).toBeNull()
    expect(readsOf('/tmp/b.md')).toBe(1)

    // The file is back (restored, or the move undone): the next return reads it
    // and the banner goes; the buffer, still the file's content, stays.
    unreadable.delete('/tmp/b.md')
    rerender(<SixTabs activePath="/tmp/a.md" onDiskContent={onDiskContent} />)
    rerender(<SixTabs activePath="/tmp/b.md" onDiskContent={onDiskContent} />)
    await waitFor(() => expect(readsOf('/tmp/b.md')).toBe(2))
    await waitFor(() => expect(screen.queryByTestId('markdown-panel-missing-file')).toBeNull())
    expect(screen.getByRole('heading', { name: '/tmp/b.md' })).toBeTruthy()
    expect(onDiskContent).not.toHaveBeenCalled()
  })

  it('reports a read that really failed once per return, and retries on the next return', async () => {
    // A read that FAILS (not a 404) is not quiet -- the panel reports it -- and
    // it is not retried on every host render either: `SidePanel` hands the panel
    // fresh callbacks on each render, so a tab left due after a failure would
    // re-read and re-report as fast as the host renders. The failure counts as
    // this return's attempt; the next return tries again.
    const onDiskContent = vi.fn()
    const { rerender } = render(
      <SixTabs activePath="/tmp/a.md" onDiskContent={onDiskContent} />, { wrapper })
    await waitFor(() => expect(openStreams()).toHaveLength(1))

    broken.add('/tmp/b.md')
    rerender(<SixTabs activePath="/tmp/b.md" onDiskContent={onDiskContent} />)
    await waitFor(() => expect(readsOf('/tmp/b.md')).toBe(1))
    // Not quiet: the panel tells the user the file could not be read.
    await waitFor(() => expect(screen.getByText(/cannot read file/i)).toBeTruthy())
    rerender(<SixTabs activePath="/tmp/b.md" onDiskContent={onDiskContent} />)
    rerender(<SixTabs activePath="/tmp/b.md" onDiskContent={onDiskContent} />)
    await settle()
    expect(readsOf('/tmp/b.md')).toBe(1)
    expect(onDiskContent).not.toHaveBeenCalled()

    rerender(<SixTabs activePath="/tmp/a.md" onDiskContent={onDiskContent} />)
    rerender(<SixTabs activePath="/tmp/b.md" onDiskContent={onDiskContent} />)
    await waitFor(() => expect(readsOf('/tmp/b.md')).toBe(2))
  })
})

const CODE = '/tmp/x.ts'
const CODE_AS_OPENED = 'export const x = 1\n'
const CODE_REWRITTEN = 'export const x = 2\n'

// A save in `EditorTabs` writes only once `releaseSaves()` runs while this is
// set -- the window in which the user keeps typing while Cmd+S is in flight.
let pendingSaves: Array<() => void> | null = null
function holdSaves() {
  pendingSaves = []
  return () => { for (const write of pendingSaves ?? []) write(); pendingSaves = null }
}

/**
 * A markdown tab and a code tab with the buffer state a host keeps: the panel's
 * `onContentChange` moves `content` (a keystroke in the editor stub goes through
 * the panel's own onChange), `onSave` writes the buffer to `disk` and restamps
 * the saved baseline the way `usePanelDocumentActions.saveFile` does,
 * `onDiskContent` restamps both and is spied on. A path in `draft` mounts with
 * that buffer ahead of its saved baseline -- an unsaved draft restored by a
 * reload, which is dirty from the first render.
 */
function EditorTabs({ activePath, onDiskContent, draft = {} }: {
  activePath: string
  onDiskContent: (path: string, text: string, binary: boolean) => void
  draft?: Record<string, string>
}) {
  const [buffers, setBuffers] = useState<Record<string, { content: string; saved: string }>>(() => ({
    '/tmp/a.md': { content: draft['/tmp/a.md'] ?? bodyOf('/tmp/a.md'), saved: bodyOf('/tmp/a.md') },
    [CODE]: { content: draft[CODE] ?? CODE_AS_OPENED, saved: CODE_AS_OPENED },
  }))
  const patch = (path: string, next: Partial<{ content: string; saved: string }>) =>
    setBuffers(b => ({ ...b, [path]: { ...b[path], ...next } }))
  return (
    <>
      {Object.keys(buffers).map(path => (
        <div key={path} style={{ display: path === activePath ? 'block' : 'none' }}>
          <MarkdownPanel
            embedded liveWatch active={path === activePath}
            filePath={path} content={buffers[path].content} savedBaseline={buffers[path].saved}
            onContentChange={c => patch(path, { content: c })}
            onDiskContent={(text, binary) => { patch(path, { content: text, saved: text }); onDiskContent(path, text, binary) }}
            onSave={async (p, c) => {
              if (pendingSaves) await new Promise<void>(write => pendingSaves?.push(write))
              disk.set(p, c); patch(p, { saved: c })
            }}
            onClose={() => {}}
          />
        </div>
      ))}
    </>
  )
}

describe('MarkdownPanel — the first local edit fences the disk read', () => {
  it('withdraws the catch-up read the user has edited past, so a save is not restamped with the older revision', async () => {
    // A hidden code tab's file changes; the user activates the tab and, before
    // the catch-up read lands, types and saves. The read's bytes describe the
    // revision the tab left behind; landing after the save, when `dirty` is
    // clear again, they would restamp the just-saved text with the older one.
    // The first edit withdraws the read, and the save owes one of its own,
    // which brings back what was just written.
    const onDiskContent = vi.fn()
    disk.set(CODE, CODE_AS_OPENED)
    const { rerender } = render(<EditorTabs activePath="/tmp/a.md" onDiskContent={onDiskContent} />, { wrapper })
    await waitFor(() => expect(openStreams()).toHaveLength(1))

    disk.set(CODE, CODE_REWRITTEN)
    const land = holdReads(CODE)
    rerender(<EditorTabs activePath={CODE} onDiskContent={onDiskContent} />)
    await waitFor(() => expect(readsOf(CODE)).toBe(1))

    fireEvent.change(screen.getByTestId('pierre-editor'), { target: { value: 'export const x = 3\n' } })
    fireEvent.click(screen.getByTestId('pierre-save'))
    await waitFor(() => expect(disk.get(CODE)).toBe('export const x = 3\n'))
    land()

    // Only the post-edit read lands; it finds the text just written on screen
    // already and restamps nothing. The older revision never lands.
    await waitFor(() => expect(readsOf(CODE)).toBe(2))
    await settle()
    expect(onDiskContent).not.toHaveBeenCalled()
    expect((screen.getByTestId('pierre-editor') as HTMLTextAreaElement).value).toBe('export const x = 3\n')
  })

  it('reads again after an undo even when Refresh had overtaken the catch-up read', async () => {
    // Refresh supersedes the catch-up read (the newer read owns the answer); the
    // user types before Refresh's read lands, so that one is dropped too; then
    // undoes. With no landing left to move the latch the clean buffer would sit
    // at the pre-change revision unread, and a later save would write it over
    // the newer file. The first edit resets the latch, so the undo reads.
    const onDiskContent = vi.fn()
    disk.set(CODE, CODE_AS_OPENED)
    const { rerender } = render(<EditorTabs activePath="/tmp/a.md" onDiskContent={onDiskContent} />, { wrapper })
    await waitFor(() => expect(openStreams()).toHaveLength(1))

    disk.set(CODE, CODE_REWRITTEN)
    const land = holdReads(CODE)
    rerender(<EditorTabs activePath={CODE} onDiskContent={onDiskContent} />)
    await waitFor(() => expect(readsOf(CODE)).toBe(1))

    // The code tab's overflow menu is the second one mounted.
    fireEvent.click(screen.getAllByTestId('markdown-panel-more-options')[1])
    fireEvent.click(screen.getByRole('menuitem', { name: /refresh/i }))
    await waitFor(() => expect(readsOf(CODE)).toBe(2))

    fireEvent.change(screen.getByTestId('pierre-editor'), { target: { value: CODE_AS_OPENED + 'typed\n' } })
    land()
    await settle()
    expect(onDiskContent).not.toHaveBeenCalled()

    // Undo, byte-exact: the buffer is clean again and at the pre-change revision.
    fireEvent.change(screen.getByTestId('pierre-editor'), { target: { value: CODE_AS_OPENED } })
    await waitFor(() => expect(onDiskContent).toHaveBeenCalledWith(CODE, CODE_REWRITTEN, false))
    expect(readsOf(CODE)).toBe(3)
  })

  it('keeps typing done while a save was in flight, and does not read over it', async () => {
    // Cmd+S writes the buffer as it was when pressed. A keystroke typed before
    // the write round-trips is not in the file, so the buffer must stay dirty:
    // clearing it would let the post-edit catch-up read run and put the text
    // just written back over the newer keystroke, with no notice.
    const onDiskContent = vi.fn()
    disk.set(CODE, CODE_AS_OPENED)
    const { rerender } = render(<EditorTabs activePath="/tmp/a.md" onDiskContent={onDiskContent} />, { wrapper })
    await waitFor(() => expect(openStreams()).toHaveLength(1))
    rerender(<EditorTabs activePath={CODE} onDiskContent={onDiskContent} />)
    await waitFor(() => expect(readsOf(CODE)).toBe(1))
    await settle()
    onDiskContent.mockClear()

    const editor = screen.getByTestId('pierre-editor') as HTMLTextAreaElement
    fireEvent.change(editor, { target: { value: 'export const x = 3\n' } })
    const write = holdSaves()
    fireEvent.click(screen.getByTestId('pierre-save'))
    fireEvent.change(editor, { target: { value: 'export const x = 3\nexport const y = 4\n' } })
    write()
    await waitFor(() => expect(disk.get(CODE)).toBe('export const x = 3\n'))
    await settle()

    // The later keystroke is still on screen, nothing was read over it, and the
    // tab still knows it has unsaved work: saving again writes that text.
    expect(editor.value).toBe('export const x = 3\nexport const y = 4\n')
    expect(onDiskContent).not.toHaveBeenCalled()
    expect(readsOf(CODE)).toBe(1)
    fireEvent.click(screen.getByTestId('pierre-save'))
    await waitFor(() => expect(disk.get(CODE)).toBe('export const x = 3\nexport const y = 4\n'))
  })

  it('never restamps a save with a cached body that predates it', async () => {
    // A reload restores an unsaved draft: the cold-tab hydration reads the file
    // through the shared query (cached for 10 s) and the panel mounts DIRTY, so
    // it has not read yet. The user saves inside the window. The save clears
    // `dirty` and the catch-up read follows -- the panel's first read, which may
    // be served by the fresh cached open read. That entry holds the pre-save
    // body: applied, it would restamp the just-saved text and baseline with the
    // older revision, and the next save would write that revision back over the
    // file. The save must leave the cache saying what the disk now holds.
    const onDiskContent = vi.fn()
    disk.set(CODE, CODE_AS_OPENED)
    qc.setQueryData(['file-read', CODE], { text: CODE_AS_OPENED, ok: true, status: 200, binary: false })
    render(<EditorTabs activePath={CODE} draft={{ [CODE]: 'export const x = 3\n' }} onDiskContent={onDiskContent} />, { wrapper })
    await settle()
    // Dirty from the first render: no read.
    expect(readsOf(CODE)).toBe(0)

    fireEvent.click(screen.getByTestId('pierre-save'))
    await waitFor(() => expect(disk.get(CODE)).toBe('export const x = 3\n'))
    // The post-save catch-up has run once the cache entry is gone: every read
    // the panel adopts drops it, this one included.
    await waitFor(() => expect(qc.getQueryState(['file-read', CODE])).toBeUndefined())
    await settle()

    // The saved text is what the tab shows and what the disk holds; nothing
    // older landed. The post-save read went to the disk, not to the entry the
    // hydration left: a save ends the open read's relevance.
    expect((screen.getByTestId('pierre-editor') as HTMLTextAreaElement).value).toBe('export const x = 3\n')
    expect(onDiskContent).not.toHaveBeenCalled()
    expect(readsOf(CODE)).toBe(1)
  })

  it('re-reads when a clean tab leaves edit mode, so a file emptied while it was being edited is adopted', async () => {
    // Edit mode parks the stream (a live push would land under the cursor) and
    // the tab is clean, so nothing is typed and nothing tells the tab about the
    // disk. Meanwhile the file is truncated to zero bytes. Leaving edit mode
    // re-arms the stream -- whose first frame never comes: `api_file_watch`
    // emits only when the content differs from its initial empty string, and
    // the content IS the empty string. Left to the stream, the tab would show
    // the old text as clean and current, and a later save would write it back
    // over the emptied file. Leaving edit mode re-arms the catch-up read too.
    const onDiskContent = vi.fn()
    render(<EditorTabs activePath="/tmp/a.md" onDiskContent={onDiskContent} />, { wrapper })
    await waitFor(() => expect(readsOf('/tmp/a.md')).toBe(1))
    await waitFor(() => expect(openStreams()).toHaveLength(1))

    // Only the markdown tab has an Edit / Preview toggle (the code tab is
    // always in the editor).
    fireEvent.click(screen.getByRole('button', { name: /^edit$/i }))
    await waitFor(() => expect(openStreams()).toHaveLength(0))
    disk.set('/tmp/a.md', '')

    fireEvent.click(screen.getByRole('button', { name: /^preview$/i }))
    await waitFor(() => expect(openStreams()).toHaveLength(1))
    // The stream stays silent (the stub fires nothing on its own); the read is
    // what brings the empty file in.
    await waitFor(() => expect(onDiskContent).toHaveBeenCalledWith('/tmp/a.md', '', false))
    expect(readsOf('/tmp/a.md')).toBe(2)
    // Entering edit mode cost no read: the tab was covered by the stream up to
    // that moment, and the re-read belongs to the return from it.
    expect(onDiskContent).toHaveBeenCalledTimes(1)
  })
})

describe('MarkdownPanel — every catch-up read is a fresh fetch', () => {
  it('reads the disk on mount while the read that opened the tab is still fresh in the cache, so a file deleted since is found', async () => {
    // `openFile` reads the file through the shared `['file-read', path]` query
    // and the tab mounts a moment later. Between the two the file can be
    // deleted -- the agent removes a file the user has just clicked open -- and
    // the cached body says nothing about that. Served from it, the mount
    // counted as caught up, the stream 404'd into a silent terminal error, and
    // the edit and save that followed recreated the deleted file from the
    // stale copy. The mount reads the disk and finds the deletion.
    const onDiskContent = vi.fn()
    qc.setQueryData(['file-read', '/tmp/c.md'], { text: bodyOf('/tmp/c.md'), ok: true, status: 200, binary: false })
    unreadable.add('/tmp/c.md')
    render(<SixTabs activePath="/tmp/c.md" onDiskContent={onDiskContent} />, { wrapper })
    await waitFor(() => expect(screen.getByTestId('markdown-panel-missing-file')).toBeTruthy())
    await settle()
    expect(freshReadsOf('/tmp/c.md')).toBe(1)
    expect(readsOf('/tmp/c.md')).toBe(1)
    expect(onDiskContent).not.toHaveBeenCalled()
    // The body the read proved stale did not outlive it.
    expect(qc.getQueryState(['file-read', '/tmp/c.md'])).toBeUndefined()
  })

  it('fetches once per mount, per return and per save, and never from the cache, however fresh the entry', async () => {
    // The one rule the catch-up rests on. A body that is not this read's --
    // the opening read, an earlier catch-up, a save's echo -- can predate the
    // change the read exists to find, so no catch-up is served from
    // `['file-read', path]`: each is a call of `fetchFileRead`, the network
    // sees each one, and the entry is gone once the read has landed.
    const onDiskContent = vi.fn()
    const seed = () => qc.setQueryData(['file-read', CODE], { text: disk.get(CODE), ok: true, status: 200, binary: false })
    disk.set(CODE, CODE_AS_OPENED)
    seed()
    const { rerender } = render(<EditorTabs activePath={CODE} onDiskContent={onDiskContent} />, { wrapper })
    await waitFor(() => expect(freshReadsOf(CODE)).toBe(1))      // the mount

    seed()
    rerender(<EditorTabs activePath="/tmp/a.md" onDiskContent={onDiskContent} />)
    rerender(<EditorTabs activePath={CODE} onDiskContent={onDiskContent} />)
    await waitFor(() => expect(freshReadsOf(CODE)).toBe(2))      // the return

    seed()
    fireEvent.change(screen.getByTestId('pierre-editor'), { target: { value: 'export const x = 3\n' } })
    fireEvent.click(screen.getByTestId('pierre-save'))
    await waitFor(() => expect(freshReadsOf(CODE)).toBe(3))      // the post-save read
    await settle()

    expect(readsOf(CODE)).toBe(freshReadsOf(CODE))
    expect(qc.getQueryState(['file-read', CODE])).toBeUndefined()
    expect((screen.getByTestId('pierre-editor') as HTMLTextAreaElement).value).toBe('export const x = 3\n')
    expect(onDiskContent).not.toHaveBeenCalled()
  })
})

describe('MarkdownPanel — the re-armed stream and the catch-up read cost one read', () => {
  it('reads once per activation when the stream\'s first frame repeats what the read brought', async () => {
    // `api_file_watch` starts from mtime 0, so a re-armed stream's first poll
    // tick always writes a frame with the whole body. Re-reading on that frame
    // made every tab switch two GETs. A frame whose text IS the clean buffer
    // says nothing the tab does not already show, so it costs no read; a frame
    // that differs is a real change and still re-reads.
    const onDiskContent = vi.fn()
    disk.set(CODE, CODE_AS_OPENED)
    const { rerender } = render(<EditorTabs activePath={CODE} onDiskContent={onDiskContent} />, { wrapper })
    await waitFor(() => expect(readsOf(CODE)).toBe(1))

    disk.set('/tmp/a.md', '# /tmp/a.md\n\nrewritten while hidden\n')
    rerender(<EditorTabs activePath="/tmp/a.md" onDiskContent={onDiskContent} />)
    await waitFor(() => expect(onDiskContent).toHaveBeenCalledWith(
      '/tmp/a.md', '# /tmp/a.md\n\nrewritten while hidden\n', false))
    await waitFor(() => expect(openStreams()).toHaveLength(1))

    // The server's connect-time frame: the same body the catch-up read applied.
    act(() => { openStreams()[0].onmessage?.({ data: JSON.stringify({ content: disk.get('/tmp/a.md') }) }) })
    await settle()
    expect(readsOf('/tmp/a.md')).toBe(1)
    expect(onDiskContent).toHaveBeenCalledTimes(1)

    // A later frame carrying a real change re-reads, as before.
    disk.set('/tmp/a.md', '# /tmp/a.md\n\nchanged again\n')
    act(() => { openStreams()[0].onmessage?.({ data: JSON.stringify({ content: disk.get('/tmp/a.md') }) }) })
    await waitFor(() => expect(onDiskContent).toHaveBeenCalledWith('/tmp/a.md', '# /tmp/a.md\n\nchanged again\n', false))
    expect(readsOf('/tmp/a.md')).toBe(2)
  })

  it('re-reads on an equal frame while a read is still in flight', async () => {
    // The tab shows A, the file is B, and the activation read snapshots B --
    // then the file goes back to A and the stream says so before that read
    // lands. Equality with the displayed A must not silence the frame: the
    // in-flight read would land last and make B the clean baseline while the
    // disk holds A, and a later save would write B over it. The frame
    // supersedes the read instead; the fresh read finds A and restamps nothing.
    const onDiskContent = vi.fn()
    disk.set(CODE, CODE_AS_OPENED)
    const { rerender } = render(<EditorTabs activePath={CODE} onDiskContent={onDiskContent} />, { wrapper })
    await waitFor(() => expect(readsOf(CODE)).toBe(1))

    disk.set('/tmp/a.md', '# /tmp/a.md\n\nrewritten while hidden\n')
    const land = holdReads('/tmp/a.md')
    rerender(<EditorTabs activePath="/tmp/a.md" onDiskContent={onDiskContent} />)
    await waitFor(() => expect(readsOf('/tmp/a.md')).toBe(1))
    await waitFor(() => expect(openStreams()).toHaveLength(1))

    disk.set('/tmp/a.md', bodyOf('/tmp/a.md'))
    act(() => { openStreams()[0].onmessage?.({ data: JSON.stringify({ content: bodyOf('/tmp/a.md') }) }) })
    await waitFor(() => expect(readsOf('/tmp/a.md')).toBe(2))
    land()
    await settle()
    expect(onDiskContent).not.toHaveBeenCalled()
  })

  it('drops the cached file-read entry even when the read matches the tab', async () => {
    // Open (cached for 10 s), edit, save inside that window: the post-edit read
    // finds what the tab shows and restamps nothing -- but the cache still holds
    // the OLD body, and a reopen of the same path inside the window would
    // rehydrate it over the saved text. Every adopted read drops the entry.
    const onDiskContent = vi.fn()
    disk.set(CODE, CODE_AS_OPENED)
    render(<EditorTabs activePath={CODE} onDiskContent={onDiskContent} />, { wrapper })
    await waitFor(() => expect(readsOf(CODE)).toBe(1))
    await settle()
    qc.setQueryData(['file-read', CODE], { text: CODE_AS_OPENED, ok: true, status: 200, binary: false })

    fireEvent.change(screen.getByTestId('pierre-editor'), { target: { value: 'export const x = 3\n' } })
    fireEvent.click(screen.getByTestId('pierre-save'))
    await waitFor(() => expect(readsOf(CODE)).toBe(2))
    await settle()
    expect(onDiskContent).not.toHaveBeenCalled()
    expect(qc.getQueryState(['file-read', CODE])).toBeUndefined()
  })
})

/**
 * One visible tab whose file is gone from disk, with the props a host gives a
 * tab: `content` is what the tab store holds -- real text, the "file not
 * found" placeholder a chip click on a deleted path seeds, or the empty text
 * buffer of a binary tab -- and `onClose` is what X / Escape reach.
 */
function LastCopyTab({ content, binary = false, partial = false, onClose = () => {}, panelRef, filePath = '/tmp/b.md' }: {
  content: string; binary?: boolean; partial?: boolean; onClose?: () => void; filePath?: string
  panelRef?: React.Ref<import('../components/MarkdownPanel').MarkdownPanelHandle>
}) {
  return (
    <MarkdownPanel
      ref={panelRef}
      embedded liveWatch active
      filePath={filePath} content={content} savedBaseline={content} binary={binary} partial={partial}
      onContentChange={() => {}} onDiskContent={() => {}}
      onSave={async () => {}} onClose={onClose}
    />
  )
}

describe('MarkdownPanel — the last copy of a file that is gone', () => {
  it('raises no banner over a buffer that holds no copy: the placeholder a chip click seeded, or a binary tab', async () => {
    // A chip click or a reload on a path that is already missing seeds the tab
    // with the "file not found" sentence (`openFile`, the cold-tab hydration),
    // and a binary tab's text buffer is empty. The catch-up read answers 404
    // for both, and neither is a copy of anything: a banner there would offer
    // to download the placeholder prose under the file's name.
    unreadable.add('/tmp/b.md')
    const placeholder = i18nT('pages.chatPage.file_not_found_on_disk_it_may_have_been_moved_or')
    const { unmount } = render(<LastCopyTab content={placeholder} />, { wrapper })
    await waitFor(() => expect(readsOf('/tmp/b.md')).toBe(1))
    await settle()
    expect(screen.queryByTestId('markdown-panel-missing-file')).toBeNull()
    expect(screen.queryByText(/cannot read file/i)).toBeNull()
    unmount()

    render(<LastCopyTab content="" binary />, { wrapper })
    await waitFor(() => expect(readsOf('/tmp/b.md')).toBe(2))
    await settle()
    expect(screen.queryByTestId('markdown-panel-missing-file')).toBeNull()
  })

  it('asks before closing the tab, since a clean buffer can still be the only copy', async () => {
    // The close guard used to confirm only a DIRTY buffer, and a last copy is
    // clean: X or Escape discarded, silently, what the banner had just called
    // the last copy. The gone file is the same stakes as unsaved edits.
    const onClose = vi.fn()
    unreadable.add('/tmp/b.md')
    render(<LastCopyTab content={bodyOf('/tmp/b.md')} onClose={onClose} />, { wrapper })
    await waitFor(() => expect(screen.getByTestId('markdown-panel-missing-file')).toBeTruthy())

    fireEvent.keyDown(document, { key: 'Escape' })
    const dialog = await screen.findByRole('dialog')
    // The body names the file (the title is one truncating line): with several
    // tabs open, an unnamed target under a red button risks the wrong buffer.
    expect(within(dialog).getByText(i18nT('components.markdownPanel.close_last_copy'))).toBeTruthy()
    expect(within(dialog).getByText(i18nT('components.markdownPanel.close_last_copy_body', { name: 'b.md' }))).toBeTruthy()
    expect(onClose).not.toHaveBeenCalled()
    // Declined: the tab, and the copy, stay.
    fireEvent.click(within(dialog).getByRole('button', { name: i18nT('components.confirmDialog.cancel') }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('heading', { name: '/tmp/b.md' })).toBeTruthy()

    // Confirmed: the close goes through, once.
    fireEvent.keyDown(document, { key: 'Escape' })
    const again = await screen.findByRole('dialog')
    fireEvent.click(within(again).getByRole('button', { name: i18nT('components.markdownPanel.close_last_copy_button') }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })

  it('asks with the last-copy prompt, not the unsaved-edits one, once the last copy has been edited', async () => {
    // "Discard unsaved changes?" implies a saved version survives the close.
    // With the file gone, none does: the file being gone decides the prompt,
    // dirty or not.
    const onClose = vi.fn()
    unreadable.add(CODE)
    render(<LastCopyTab filePath={CODE} content={CODE_AS_OPENED} onClose={onClose} />, { wrapper })
    await waitFor(() => expect(screen.getByTestId('markdown-panel-missing-file')).toBeTruthy())
    fireEvent.change(screen.getByTestId('pierre-editor'), { target: { value: CODE_AS_OPENED + 'edited\n' } })

    fireEvent.keyDown(document, { key: 'Escape' })
    const dialog = await screen.findByRole('dialog')
    // The prompt names both stakes: the last copy AND the edits in it.
    expect(within(dialog).getByText(i18nT('components.markdownPanel.close_last_copy_dirty_body', { name: 'x.ts' }))).toBeTruthy()
    expect(within(dialog).queryByText(i18nT('components.markdownPanel.discard_unsaved_changes'))).toBeNull()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('keeps looking for a gone file while its tab is visible, and adopts it the moment it is back', async () => {
    // A stream cannot watch a path that is not there: `api_file_watch` answers
    // 404 and `useFileWatch` closes the stream for good. Armed while the file
    // was gone, the watch would never see the file recreated -- by the agent,
    // a branch switch, an undo of the deletion -- and the tab would keep
    // showing "file not found" over a buffer a later save would write over the
    // recreated content. So the watch is parked while the file is gone, the
    // tab re-reads on a cadence instead, and the read that finds the file
    // clears the banner and arms the stream again.
    const onDiskContent = vi.fn()
    unreadable.add('/tmp/b.md')
    render(
      <MarkdownPanel
        embedded liveWatch active filePath="/tmp/b.md" content={bodyOf('/tmp/b.md')} savedBaseline={bodyOf('/tmp/b.md')}
        onContentChange={() => {}} onDiskContent={(t, b) => onDiskContent('/tmp/b.md', t, b)} onSave={async () => {}} onClose={() => {}}
      />, { wrapper })
    await waitFor(() => expect(screen.getByTestId('markdown-panel-missing-file')).toBeTruthy())
    await settle()
    expect(openStreams()).toHaveLength(0)
    const readsWhileGone = readsOf('/tmp/b.md')

    // The file comes back with new content. Nothing else says so: no stream,
    // no user action.
    unreadable.delete('/tmp/b.md')
    disk.set('/tmp/b.md', '# /tmp/b.md\n\nrecreated\n')
    await waitFor(() => expect(onDiskContent).toHaveBeenCalledWith('/tmp/b.md', '# /tmp/b.md\n\nrecreated\n', false), { timeout: 8000 })
    await waitFor(() => expect(screen.queryByTestId('markdown-panel-missing-file')).toBeNull())
    await waitFor(() => expect(openStreams()).toHaveLength(1))
    expect(watchedPath(openStreams()[0])).toBe('/tmp/b.md')
    expect(readsOf('/tmp/b.md')).toBeGreaterThan(readsWhileGone)
  })

  it('checks the disk once before closing a clean tab under a live stream, since a deletion under the stream is silent', async () => {
    // `api_file_watch` polls on through a FileNotFoundError without emitting,
    // so a file deleted while its tab was on screen -- stream live, buffer
    // clean -- has told the panel nothing. The close is the last moment the
    // copy can still be kept, so a clean live-watched tab asks the disk once
    // there; the read that finds the file gone raises the banner and the
    // last-copy prompt instead of discarding the sole buffer unasked.
    const onClose = vi.fn()
    render(<LastCopyTab content={bodyOf('/tmp/b.md')} onClose={onClose} />, { wrapper })
    await waitFor(() => expect(readsOf('/tmp/b.md')).toBe(1))
    await waitFor(() => expect(openStreams()).toHaveLength(1))
    await settle()
    expect(screen.queryByTestId('markdown-panel-missing-file')).toBeNull()

    unreadable.add('/tmp/b.md')                                 // deleted; the stream says nothing
    fireEvent.keyDown(document, { key: 'Escape' })
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(i18nT('components.markdownPanel.close_last_copy_body', { name: 'b.md' }))).toBeTruthy()
    expect(onClose).not.toHaveBeenCalled()
    expect(readsOf('/tmp/b.md')).toBe(2)
    expect(screen.getByTestId('markdown-panel-missing-file')).toBeTruthy()

    // A file that is still there closes without a prompt, as before.
    fireEvent.click(within(dialog).getByRole('button', { name: i18nT('components.confirmDialog.cancel') }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    unreadable.delete('/tmp/b.md')
    await waitFor(() => expect(screen.queryByTestId('markdown-panel-missing-file')).toBeNull(), { timeout: 8000 })
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('makes the ⋯ menu\'s Download hand over the buffer while the file is gone, not a failed fetch of the path', async () => {
    // The banner says "download it"; the menu's Download fetched the path and
    // answered "Download failed" for exactly that file. While the file is gone
    // the row serves the buffer, the same bytes the banner's own button saves.
    const blobs: Blob[] = []
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: (b: Blob) => { blobs.push(b); return 'blob:last-copy' } })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: () => {} })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    try {
      unreadable.add('/tmp/b.md')
      render(<LastCopyTab content={bodyOf('/tmp/b.md')} />, { wrapper })
      await screen.findByTestId('markdown-panel-missing-file')
      fireEvent.click(screen.getByTestId('markdown-panel-more-options'))
      fireEvent.click(screen.getByRole('menuitem', { name: i18nT('components.markdownPanel.download') }))
      expect(blobs).toHaveLength(1)
      expect(await blobs[0].text()).toBe(bodyOf('/tmp/b.md'))
      expect(vi.mocked(fetch).mock.calls.filter(([u]) => String(u).startsWith('/api/file-download'))).toHaveLength(0)
      expect(screen.queryByText(/download failed/i)).toBeNull()
    } finally {
      click.mockRestore()
      delete (URL as unknown as Record<string, unknown>).createObjectURL
      delete (URL as unknown as Record<string, unknown>).revokeObjectURL
    }
  })

  it('does not close on a close-time read the user typed past: the keystroke stays, and the next close asks about it', async () => {
    // Escape on a clean live-watched tab starts the disk check; the user types
    // before it answers. The `dirty` the guard captured is stale, and the
    // typing aborted the read: closing on either would discard the keystroke.
    // Nothing closes; the next Escape sees the dirty buffer and asks.
    const onClose = vi.fn()
    render(<LastCopyTab filePath={CODE} content={CODE_AS_OPENED} onClose={onClose} />, { wrapper })
    await waitFor(() => expect(readsOf(CODE)).toBe(1))
    await settle()

    const land = holdReads(CODE)
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(readsOf(CODE)).toBe(2))
    fireEvent.change(screen.getByTestId('pierre-editor'), { target: { value: CODE_AS_OPENED + 'typed\n' } })
    land()
    await settle()
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).toBeNull()

    fireEvent.keyDown(document, { key: 'Escape' })
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(i18nT('components.markdownPanel.discard_unsaved_changes'))).toBeTruthy()
    expect(onClose).not.toHaveBeenCalled()
  })

  // What the gateway hands a tab for a file it could not serve whole: a body
  // cut at its character cap (`X-Truncated`), or one with the redaction pass's
  // tag in place of a secret (`X-Redacted`). The verdict is the HOST's, stamped
  // from the read's headers by whichever read filled the buffer -- here the
  // opening read, before this panel ever mounted -- and the body says nothing:
  // a whole file may quote the tag verbatim.
  const QUOTES_THE_TAG = bodyOf('/tmp/b.md') + 'token = [REDACTED: credential]\n'
  for (const [shape, partial, body] of [['partial', true, bodyOf('/tmp/b.md')], ['whole, tag-quoting', false, QUOTES_THE_TAG]] as const) {
    it(`names a ${shape} buffer for what the read said it was: banner, Download name and the close prompt's words`, async () => {
      // A partial buffer is not the whole file: the banner must not call it the
      // file's remaining contents, and a download of it must not pass a prefix
      // off under the file's own name once the original is gone. It is still
      // the most that survives, so the close asks about it exactly as it asks
      // about a whole copy -- only the words change. A whole buffer that merely
      // contains the tag is whole: same guard, the plain words, the file's own
      // name -- nothing about its shape is consulted.
      const onClose = vi.fn()
      const blobs: Blob[] = []
      const names: string[] = []
      const suffix = partial ? '.partial' : ''
      Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: (b: Blob) => { blobs.push(b); return 'blob:partial' } })
      Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: () => {} })
      const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { names.push(this.download) })
      try {
        unreadable.add('/tmp/b.md')
        render(<LastCopyTab content={body} partial={partial} onClose={onClose} />, { wrapper })
        const banner = await screen.findByTestId('markdown-panel-missing-file')
        if (partial) expect(banner.textContent).toMatch(/only part of it/i)
        else expect(banner.textContent).toMatch(/nothing else holds/i)
        expect(banner.textContent).not.toMatch(partial ? /nothing else holds/i : /only part of it/i)

        fireEvent.click(within(banner).getByRole('button', { name: i18nT('components.markdownPanel.download') }))
        expect(names).toEqual([`b.md${suffix}`])
        expect(blobs[0].size).toBe(new TextEncoder().encode(body).length)

        fireEvent.keyDown(document, { key: 'Escape' })
        const dialog = await screen.findByRole('dialog')
        expect(within(dialog).getByText(i18nT(partial ? 'components.markdownPanel.close_partial_copy_body' : 'components.markdownPanel.close_last_copy_body', { name: 'b.md' }))).toBeTruthy()
        expect(onClose).not.toHaveBeenCalled()
        // The dialog's own rescue hands over the same buffer, under the same name.
        fireEvent.click(screen.getByTestId('markdown-panel-dialog-download'))
        expect(names).toEqual([`b.md${suffix}`, `b.md${suffix}`])
        fireEvent.click(within(dialog).getByRole('button', { name: i18nT('components.markdownPanel.close_last_copy_button') }))
        await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
      } finally {
        click.mockRestore()
        delete (URL as unknown as Record<string, unknown>).createObjectURL
        delete (URL as unknown as Record<string, unknown>).revokeObjectURL
      }
    })
  }

  it('carries the read\'s own partial verdict to the host with the text, so the tab is marked from the headers', async () => {
    // The panel's reads move the verdict the way they move the buffer: a read
    // answering `X-Truncated` (or `X-Redacted`) hands the host `partial: true`
    // beside the text, and a later read that comes back whole hands it
    // `false` -- even when the text itself did not change, since the
    // provenance did.
    const onDiskContent = vi.fn()
    partialReads.add('/tmp/b.md')
    disk.set('/tmp/b.md', 'a prefix of b\n')
    render(
      <MarkdownPanel embedded liveWatch active filePath="/tmp/b.md" content={bodyOf('/tmp/b.md')} savedBaseline={bodyOf('/tmp/b.md')}
        onContentChange={() => {}} onDiskContent={onDiskContent} onSave={async () => {}} onClose={() => {}} />,
      { wrapper })
    await waitFor(() => expect(onDiskContent).toHaveBeenCalledWith('a prefix of b\n', false, true))
  })

  it('marks a lossily decoded read partial too: a Latin-1 file shown with replacement characters is not the file as written', async () => {
    const onDiskContent = vi.fn()
    lossyReads.add('/tmp/b.md')
    disk.set('/tmp/b.md', 'caf\ufffd au lait\n')
    render(
      <MarkdownPanel embedded liveWatch active filePath="/tmp/b.md" content={bodyOf('/tmp/b.md')} savedBaseline={bodyOf('/tmp/b.md')}
        onContentChange={() => {}} onDiskContent={onDiskContent} onSave={async () => {}} onClose={() => {}} />,
      { wrapper })
    await waitFor(() => expect(onDiskContent).toHaveBeenCalledWith('caf\ufffd au lait\n', false, true))
  })

  it('restamps a tab whose text is unchanged when only the partial verdict moved', async () => {
    // A truncated seed read, then the file shrank to what the tab shows: the
    // mount read finds the same text, whole. Same bytes, new provenance -- and
    // it is restamped, so the banner and a download would tell the truth.
    const onDiskContent = vi.fn()
    disk.set('/tmp/b.md', bodyOf('/tmp/b.md'))
    render(
      <MarkdownPanel embedded liveWatch active filePath="/tmp/b.md" content={bodyOf('/tmp/b.md')} savedBaseline={bodyOf('/tmp/b.md')} partial
        onContentChange={() => {}} onDiskContent={onDiskContent} onSave={async () => {}} onClose={() => {}} />,
      { wrapper })
    await waitFor(() => expect(onDiskContent).toHaveBeenCalledWith(bodyOf('/tmp/b.md'), false, false))
  })

  it('never starts a retry over a read already in flight, and backs off while the file stays gone', async () => {
    // A retry that started over the activation catch-up, the close guard or
    // the watch's re-read would abort it. A tick that finds a read out skips
    // its own. And a file that stays gone is asked for less and less often:
    // the wait doubles from one second toward a ceiling, so a tab left on a
    // deleted file costs a GET (and a SEL not_found record) every 30 s, not
    // every second.
    unreadable.add('/tmp/b.md')
    render(<LastCopyTab content={bodyOf('/tmp/b.md')} />, { wrapper })
    await waitFor(() => expect(screen.getByTestId('markdown-panel-missing-file')).toBeTruthy())
    await settle()
    const t0 = Date.now()
    const n0 = readsOf('/tmp/b.md')
    // Retry 1 at ~1 s, retry 2 at ~3 s (1 s + 2 s): by 4.6 s exactly two more.
    await waitFor(() => expect(readsOf('/tmp/b.md')).toBe(n0 + 2), { timeout: 4500 })
    expect(Date.now() - t0).toBeGreaterThan(2500)
    await new Promise(resolve => setTimeout(resolve, 1500))
    expect(readsOf('/tmp/b.md')).toBe(n0 + 2)

    // A read is out (Refresh's, held): the tick due meanwhile must not abort it
    // -- no new read goes out until it has landed.
    const land = holdReads('/tmp/b.md')
    fireEvent.click(screen.getByTestId('markdown-panel-more-options'))
    fireEvent.click(screen.getByRole('menuitem', { name: /refresh/i }))
    await waitFor(() => expect(readsOf('/tmp/b.md')).toBe(n0 + 3))
    await new Promise(resolve => setTimeout(resolve, 4500))
    expect(readsOf('/tmp/b.md')).toBe(n0 + 3)
    land()
    await settle()
    expect(screen.getByTestId('markdown-panel-missing-file')).toBeTruthy()
  })

  it('waits for each retry read to settle before the next, so a slow read is never aborted by the cadence', async () => {
    // A retry every second by the clock aborts a read slower than a second,
    // every time: a recreated file behind a slow gateway would never land, and
    // the user's first edit would then stop the retries for good. Each retry is
    // scheduled only once the previous read has settled.
    const onDiskContent = vi.fn()
    unreadable.add('/tmp/b.md')
    render(
      <MarkdownPanel
        embedded liveWatch active filePath="/tmp/b.md" content={bodyOf('/tmp/b.md')} savedBaseline={bodyOf('/tmp/b.md')}
        onContentChange={() => {}} onDiskContent={(t, b) => onDiskContent('/tmp/b.md', t, b)} onSave={async () => {}} onClose={() => {}}
      />, { wrapper })
    await waitFor(() => expect(screen.getByTestId('markdown-panel-missing-file')).toBeTruthy())
    await settle()
    const land = holdReads('/tmp/b.md')
    const before = readsOf('/tmp/b.md')
    // The first retry goes out and hangs; a second tick must NOT follow it.
    await waitFor(() => expect(readsOf('/tmp/b.md')).toBe(before + 1), { timeout: 3000 })
    await new Promise(resolve => setTimeout(resolve, 2500))
    expect(readsOf('/tmp/b.md')).toBe(before + 1)
    // The file is back while that slow read is still out (the stub answers a
    // held read with the file as it was when the request went out, so this one
    // lands as a 200 of the same body). Released, it is the read that lands --
    // not a successor that aborted it -- and it is what clears the banner and
    // arms the stream; no further read went out meanwhile.
    unreadable.delete('/tmp/b.md')
    land()
    await waitFor(() => expect(screen.queryByTestId('markdown-panel-missing-file')).toBeNull(), { timeout: 5000 })
    await waitFor(() => expect(openStreams()).toHaveLength(1))
    expect(readsOf('/tmp/b.md')).toBe(before + 1)
  })

  it('asks "close anyway?" when the close-time disk check itself failed, instead of refusing the close', async () => {
    // A gateway that is down cannot say whether the file is there. Refusing
    // the close silently would make the tab impossible to close; the question
    // goes to the user, with the Download way out named.
    const onClose = vi.fn()
    render(<LastCopyTab content={bodyOf('/tmp/b.md')} onClose={onClose} />, { wrapper })
    await waitFor(() => expect(readsOf('/tmp/b.md')).toBe(1))
    await settle()
    broken.add('/tmp/b.md')
    fireEvent.keyDown(document, { key: 'Escape' })
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(i18nT('components.markdownPanel.close_unverified'))).toBeTruthy()
    expect(within(dialog).getByText(i18nT('components.markdownPanel.close_unverified_body', { name: 'b.md' }))).toBeTruthy()
    expect(onClose).not.toHaveBeenCalled()
    // The dialog IS the report of that failure: no second telling through the
    // action notice, which would outlive a Cancel and sit on the tab.
    expect(screen.queryByText(/cannot read file/i)).toBeNull()
    fireEvent.click(within(dialog).getByRole('button', { name: i18nT('components.markdownPanel.close_unverified_button') }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })

  it('does not ask "close anyway?" over a buffer that could not be a last copy -- empty, or the seeded placeholder', async () => {
    // The question exists to protect a copy. An empty buffer and the "file not
    // found" sentence the host seeds are not one, so a read that failed at
    // close has nothing to ask about: telling such a tab it "may hold the only
    // remaining version" would be false, and one more dialog on the way out.
    for (const content of ['', i18nT('pages.chatPage.file_not_found_on_disk_it_may_have_been_moved_or')]) {
      const onClose = vi.fn()
      const { unmount } = render(<LastCopyTab content={content} onClose={onClose} />, { wrapper })
      await waitFor(() => expect(readsOf('/tmp/b.md')).toBeGreaterThan(0))
      await settle()
      broken.add('/tmp/b.md')
      fireEvent.keyDown(document, { key: 'Escape' })
      await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
      expect(screen.queryByRole('dialog')).toBeNull()
      broken.delete('/tmp/b.md')
      unmount()
    }
  })

  it('checks the disk at close for a dirty tab too, and names both the gone file and the edits', async () => {
    // A dirty tab reads nothing on its own, so a file deleted under it is
    // unknown to the panel until the close asks; "Discard unsaved changes?"
    // alone would imply a saved version survives.
    const onClose = vi.fn()
    render(<LastCopyTab filePath={CODE} content={CODE_AS_OPENED} onClose={onClose} />, { wrapper })
    await waitFor(() => expect(readsOf(CODE)).toBe(1))
    await settle()
    fireEvent.change(screen.getByTestId('pierre-editor'), { target: { value: CODE_AS_OPENED + 'edited\n' } })
    unreadable.add(CODE)
    fireEvent.keyDown(document, { key: 'Escape' })
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(i18nT('components.markdownPanel.close_last_copy_dirty_body', { name: 'x.ts' }))).toBeTruthy()
    expect(within(dialog).queryByText(i18nT('components.markdownPanel.discard_unsaved_changes'))).toBeNull()
    expect(onClose).not.toHaveBeenCalled()
    // The 404 that read answered is a fact about the FILE, not about the
    // edits: cancelling the close leaves the tab knowing its file is gone --
    // the banner stands over the edited buffer, with its own Download.
    fireEvent.click(within(dialog).getByRole('button', { name: i18nT('components.confirmDialog.cancel') }))
    const banner = await screen.findByTestId('markdown-panel-missing-file')
    expect(within(banner).getByRole('button', { name: i18nT('components.markdownPanel.download') })).toBeTruthy()
    // The edits are still unsaved: the next close asks about them again.
    fireEvent.keyDown(document, { key: 'Escape' })
    const again = await screen.findByRole('dialog')
    expect(within(again).getByText(i18nT('components.markdownPanel.close_last_copy_dirty_body', { name: 'x.ts' }))).toBeTruthy()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('answers Refresh on a gone file with the banner, not the "Cannot read file" notice', async () => {
    // A 404 is a verdict about the file, and the panel has a place for that
    // verdict: the banner, with the exits that keep the buffer. The notice is
    // for a read that could not answer at all.
    render(<LastCopyTab content={bodyOf('/tmp/b.md')} />, { wrapper })
    await waitFor(() => expect(readsOf('/tmp/b.md')).toBe(1))
    await settle()
    unreadable.add('/tmp/b.md')
    fireEvent.click(screen.getByTestId('markdown-panel-more-options'))
    fireEvent.click(screen.getByRole('menuitem', { name: /refresh/i }))
    await screen.findByTestId('markdown-panel-missing-file')
    expect(screen.queryByText(/cannot read file/i)).toBeNull()
  })

  it('answers Cancel on a gone file with the banner over the still-dirty buffer, not the notice', async () => {
    // Cancel means "match the disk"; with no disk to match, the edits are all
    // there is. They stay, marked unsaved, and the banner says why the disk
    // could not be matched.
    render(<LastCopyTab filePath={CODE} content={CODE_AS_OPENED} />, { wrapper })
    await waitFor(() => expect(readsOf(CODE)).toBe(1))
    await settle()
    fireEvent.change(screen.getByTestId('pierre-editor'), { target: { value: CODE_AS_OPENED + 'edited\n' } })
    unreadable.add(CODE)
    fireEvent.click(screen.getByTitle(i18nT('components.markdownPanel.cancel_discard_unsaved_edits')))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: i18nT('components.markdownPanel.discard_changes_button') }))
    await screen.findByTestId('markdown-panel-missing-file')
    expect(screen.queryByText(/cannot read file/i)).toBeNull()
    // Still dirty: a close now asks about the edits AND the gone file.
    fireEvent.keyDown(document, { key: 'Escape' })
    const close = await screen.findByRole('dialog')
    expect(within(close).getByText(i18nT('components.markdownPanel.close_last_copy_dirty_body', { name: 'x.ts' }))).toBeTruthy()
  })

  it('tracks a gone EMPTY file as missing -- no banner, no stream, but the retry adopts the recreation', async () => {
    // An empty buffer is no last copy, so no banner and no close guard. The
    // file is gone all the same: the stream would 404 into silence and never
    // see it recreated, so the missing state (parked watch, retry) does not
    // depend on the buffer -- only the banner and the guards do.
    const onDiskContent = vi.fn()
    disk.set('/tmp/b.md', '')
    render(
      <MarkdownPanel
        embedded liveWatch active filePath="/tmp/b.md" content="" savedBaseline=""
        onContentChange={() => {}} onDiskContent={(t, b) => onDiskContent('/tmp/b.md', t, b)} onSave={async () => {}} onClose={() => {}}
      />, { wrapper })
    await waitFor(() => expect(readsOf('/tmp/b.md')).toBe(1))
    await waitFor(() => expect(openStreams()).toHaveLength(1))

    // Deleted; the stream's next frame is the change that triggers the read.
    unreadable.add('/tmp/b.md')
    act(() => { openStreams()[0].onmessage?.({ data: JSON.stringify({ content: 'x' }) }) })
    await waitFor(() => expect(readsOf('/tmp/b.md')).toBe(2))
    await waitFor(() => expect(openStreams()).toHaveLength(0))
    expect(screen.queryByTestId('markdown-panel-missing-file')).toBeNull()

    // Recreated with content: the retry finds it, applies it, re-arms the stream.
    unreadable.delete('/tmp/b.md')
    disk.set('/tmp/b.md', '# back\n')
    await waitFor(() => expect(onDiskContent).toHaveBeenCalledWith('/tmp/b.md', '# back\n', false), { timeout: 8000 })
    await waitFor(() => expect(openStreams()).toHaveLength(1))
  })

  it('offers Download inside the last-copy dialog, and it saves the buffer without closing the dialog', async () => {
    // The body used to say "cancel and use Download first" with no Download
    // in reach. The rescue rides in the dialog now.
    const onClose = vi.fn()
    const names: string[] = []
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: () => 'blob:last-copy' })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: () => {} })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { names.push(this.download) })
    try {
      unreadable.add('/tmp/b.md')
      render(<LastCopyTab content={bodyOf('/tmp/b.md')} onClose={onClose} />, { wrapper })
      await screen.findByTestId('markdown-panel-missing-file')
      fireEvent.keyDown(document, { key: 'Escape' })
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(within(dialog).getByTestId('markdown-panel-dialog-download'))
      expect(names).toEqual(['b.md'])
      expect(screen.getByRole('dialog')).toBeTruthy()
      expect(onClose).not.toHaveBeenCalled()
    } finally {
      click.mockRestore()
      delete (URL as unknown as Record<string, unknown>).createObjectURL
      delete (URL as unknown as Record<string, unknown>).revokeObjectURL
    }
  })

  it('opens the next file beside the tab instead of re-targeting it, the way a dirty tab is spared', async () => {
    // A chip click while the tab is active re-targets a CLEAN tab in place; the
    // host asks the panel's predicate after its own read. A last copy answers
    // no, as unsaved edits do, so the new file opens as its own tab and the copy
    // stays where it is.
    const ref = createRef<import('../components/MarkdownPanel').MarkdownPanelHandle>()
    unreadable.add('/tmp/b.md')
    render(<LastCopyTab content={bodyOf('/tmp/b.md')} panelRef={ref} />, { wrapper })
    await waitFor(() => expect(screen.getByTestId('markdown-panel-missing-file')).toBeTruthy())
    const nav = vi.fn()
    act(() => ref.current!.requestNavigate(nav))
    await waitFor(() => expect(nav).toHaveBeenCalledTimes(1))
    expect((nav.mock.calls[0][0] as () => boolean)()).toBe(false)
  })

  it('asks the disk before a rail re-target, so a file deleted under the live stream spares its tab instead of losing the only copy', async () => {
    // The watch says nothing about a deletion, so a clean visible tab's stored
    // verdict is stale; the close path reads the disk first and the re-target
    // path must too -- a bare re-target on the stale verdict would replace the
    // buffer, the only copy left, with no word. The read is the close path's:
    // the missing verdict is latched (the banner appears) and the predicate
    // answers no, so the host opens the next file beside this tab.
    const ref = createRef<import('../components/MarkdownPanel').MarkdownPanelHandle>()
    render(<LastCopyTab content={bodyOf('/tmp/b.md')} panelRef={ref} />, { wrapper })
    await waitFor(() => expect(readsOf('/tmp/b.md')).toBe(1))
    await settle()
    expect(screen.queryByTestId('markdown-panel-missing-file')).toBeNull()
    unreadable.add('/tmp/b.md')
    const nav = vi.fn()
    act(() => ref.current!.requestNavigate(nav))
    await waitFor(() => expect(nav).toHaveBeenCalledTimes(1))
    expect(readsOf('/tmp/b.md')).toBe(2)
    expect((nav.mock.calls[0][0] as () => boolean)()).toBe(false)
    await screen.findByTestId('markdown-panel-missing-file')
  })

  it('spares the tab when the pre-navigation disk check fails, and re-targets when the file is intact', async () => {
    // A read that could not answer is not a licence to discard; an intact file
    // means the buffer is replaceable and the re-target goes ahead in place.
    const ref = createRef<import('../components/MarkdownPanel').MarkdownPanelHandle>()
    render(<LastCopyTab content={bodyOf('/tmp/b.md')} panelRef={ref} />, { wrapper })
    await waitFor(() => expect(readsOf('/tmp/b.md')).toBe(1))
    await settle()
    broken.add('/tmp/b.md')
    const failed = vi.fn()
    act(() => ref.current!.requestNavigate(failed))
    await waitFor(() => expect(failed).toHaveBeenCalledTimes(1))
    expect((failed.mock.calls[0][0] as () => boolean)()).toBe(false)
    expect(screen.queryByText(/cannot read file/i)).toBeNull()
    broken.delete('/tmp/b.md')
    const intact = vi.fn()
    act(() => ref.current!.requestNavigate(intact))
    await waitFor(() => expect(intact).toHaveBeenCalledTimes(1))
    expect((intact.mock.calls[0][0] as () => boolean)()).toBe(true)
    expect(readsOf('/tmp/b.md')).toBe(3)
  })

  it('carries the two exits in the banner: Download saves the buffer under the file\'s name, Copy content copies it', async () => {
    // The banner said "copy it or save it elsewhere" and offered no way to. The
    // overflow's Download fetches the PATH (`/api/file-download`), which a gone
    // file answers 404, so the banner's own Download hands the browser the
    // buffer -- the copy the banner is about -- and Copy content puts it on
    // the clipboard. Neither touches the network.
    const blobs: Blob[] = []
    const names: string[] = []
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: (b: Blob) => { blobs.push(b); return 'blob:last-copy' } })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: () => {} })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { names.push(this.download) })
    try {
      unreadable.add('/tmp/b.md')
      render(<LastCopyTab content={bodyOf('/tmp/b.md')} />, { wrapper })
      const banner = await screen.findByTestId('markdown-panel-missing-file')

      fireEvent.click(within(banner).getByRole('button', { name: i18nT('components.markdownPanel.download') }))
      expect(names).toEqual(['b.md'])
      expect(blobs).toHaveLength(1)
      expect(await blobs[0].text()).toBe(bodyOf('/tmp/b.md'))
      expect(vi.mocked(fetch).mock.calls.filter(([u]) => String(u).startsWith('/api/file-download'))).toHaveLength(0)

      fireEvent.click(within(banner).getByRole('button', { name: i18nT('components.markdownPanel.copy_content') }))
      expect(copyToClipboard).toHaveBeenCalledWith(bodyOf('/tmp/b.md'))
      // The copy answers on the button: the reader cannot see a clipboard.
      await waitFor(() => expect(within(banner).getByRole('button', { name: i18nT('components.markdownPanel.copied') })).toBeTruthy())
      expect(screen.queryByTestId('markdown-panel-action-error')).toBeNull()

      // A refused copy is not silent: it lands in the panel's action notice,
      // right where the user is about to be invited to discard the only copy.
      vi.mocked(copyToClipboard).mockResolvedValueOnce(false)
      await waitFor(() => expect(within(banner).getByRole('button', { name: i18nT('components.markdownPanel.copy_content') })).toBeTruthy(), { timeout: 3000 })
      fireEvent.click(within(banner).getByRole('button', { name: i18nT('components.markdownPanel.copy_content') }))
      await waitFor(() => expect(screen.getByTestId('markdown-panel-action-error').textContent).toMatch(/couldn.t copy/i))
      expect(within(banner).queryByRole('button', { name: i18nT('components.markdownPanel.copied') })).toBeNull()
    } finally {
      click.mockRestore()
      delete (URL as unknown as Record<string, unknown>).createObjectURL
      delete (URL as unknown as Record<string, unknown>).revokeObjectURL
    }
  })
})
