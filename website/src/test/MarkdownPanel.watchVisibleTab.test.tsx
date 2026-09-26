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
import { forwardRef, useImperativeHandle, useState } from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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
const { default: MarkdownPanel } = await import('../components/MarkdownPanel')

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
// moved file does. Everything else the panel fetches on mount (knowledge
// config, artifact state) gets an inert answer.
const disk = new Map<string, string>()
const fileReads: string[] = []
const held = new Map<string, Array<() => void>>()
const unreadable = new Set<string>()
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
      if (unreadable.has(path)) {
        return { ok: false, status: 404, headers: { get: () => null }, text: async () => '' }
      }
      return {
        ok: true, status: 200, headers: { get: () => null },
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
    // The visible tab's buffer came from the read that opened it; being visible
    // from the start is not a reactivation and costs no extra read.
    expect(fileReads).toEqual([])
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
    // Losing visibility is not a reason to read: the tab that was hidden reads
    // nothing, and no other hidden tab does either.
    expect(readsOf('/tmp/a.md')).toBe(0)
    expect(fileReads).toEqual(['/tmp/d.md'])
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

    // Back to the first tab: it was parked while B was visible, so it reads too.
    disk.set('/tmp/a.md', '# /tmp/a.md\n\nchanged during the visit to b\n')
    rerender(<SixTabs activePath="/tmp/a.md" onDiskContent={onDiskContent} />)
    await waitFor(() => expect(onDiskContent).toHaveBeenCalledWith(
      '/tmp/a.md', '# /tmp/a.md\n\nchanged during the visit to b\n', false))
    await settle()
    expect(readsOf('/tmp/a.md')).toBe(1)
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

  it('reports an unreadable file once per return, and retries on the next return', async () => {
    // A read that FAILS is not quiet -- the panel reports it -- and it is not
    // retried on every host render either: `SidePanel` hands the panel fresh
    // callbacks on each render, so a tab left due after a failure would re-read
    // and re-report as fast as the host renders. The failure counts as this
    // return's attempt; the next return tries again.
    const onDiskContent = vi.fn()
    const { rerender } = render(
      <SixTabs activePath="/tmp/a.md" onDiskContent={onDiskContent} />, { wrapper })
    await waitFor(() => expect(openStreams()).toHaveLength(1))

    unreadable.add('/tmp/b.md')
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
 * `onDiskContent` restamps both and is spied on.
 */
function EditorTabs({ activePath, onDiskContent }: {
  activePath: string
  onDiskContent: (path: string, text: string, binary: boolean) => void
}) {
  const [buffers, setBuffers] = useState<Record<string, { content: string; saved: string }>>(() => ({
    '/tmp/a.md': { content: bodyOf('/tmp/a.md'), saved: bodyOf('/tmp/a.md') },
    [CODE]: { content: CODE_AS_OPENED, saved: CODE_AS_OPENED },
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

    // Only the post-edit read lands, and it carries the saved text.
    await waitFor(() => expect(onDiskContent).toHaveBeenCalledWith(CODE, 'export const x = 3\n', false))
    await settle()
    expect(onDiskContent).not.toHaveBeenCalledWith(CODE, CODE_REWRITTEN, false)
    expect(onDiskContent).toHaveBeenCalledTimes(1)
    expect(readsOf(CODE)).toBe(2)
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
})
