/**
 * Diff mode on a file larger than the file-read cap.
 *
 * `/api/file-read` stops at a cap and says so (`partial`), while
 * `/api/file-diff` returns the WHOLE HEAD body plus git's own patch. A clean
 * partial buffer is drawn and counted from git's patch, in the viewer and in
 * the editor alike, and that patch is refetched whenever a read of the file
 * may have seen a change the cut body cannot show.
 *
 * A redacted read is partial too. When its only change sits inside a masked
 * span, the patch is empty while git's status says `modified`, and the panel
 * says git saw a change rather than "no changes".
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, within, waitFor, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreFilePair: () => <div data-testid="pierre-diff" />,
  PierrePatch: ({ patch }: { patch: string }) => <div data-testid="pierre-patch" data-value={patch} />,
  PierreEditor: () => <div data-testid="pierre-editor" />,
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

const { api } = await import('../api/client')
const { default: MarkdownPanel } = await import('../components/MarkdownPanel')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

// HEAD: 300 lines. The read cap stops the buffer after line 100. The one real
// edit is at line 250, past the cut.
const head = Array.from({ length: 300 }, (_, i) => `line ${i + 1}`)
const headBody = head.join('\n') + '\n'
const capped = head.slice(0, 100).join('\n') + '\n'
const gitDiff = [
  'diff --git a/big.py b/big.py',
  'index 1111111..2222222 100644',
  '--- a/big.py',
  '+++ b/big.py',
  '@@ -249,3 +249,3 @@',
  ' line 249',
  '-line 250',
  '+line 250 edited',
  ' line 251',
].join('\n')
const modified = { diff: gitDiff, original: headBody, status: 'modified' as const }
const clean = { diff: '', original: headBody, status: 'clean' as const }

// A token rotated in the working tree. The read and HEAD are both served
// masked, so they match and the masked patch is empty; git's status is taken
// from the raw files and says `modified`.
const maskedBody = 'API_TOKEN = "[REDACTED: credential]"\n'
const maskedChangeOnly = { diff: '', original: maskedBody, status: 'modified' as const }
const changeNotShown = "Git reports a change to this file that the diff view can't show"

// Every /api/file-read answers with `readBody` under `readHeader`; reads that
// have LANDED are counted so a test waits on a read, not on a delay.
let readBody = capped
let readHeader: 'X-Truncated' | 'X-Redacted' = 'X-Truncated'
let readsLanded = 0
function installFetch() {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const isRead = String(url).startsWith('/api/file-read')
    return {
      ok: true, status: 200,
      headers: { get: (h: string) => (isRead && h === readHeader ? 'true' : null) },
      json: async () => ({ enabled: false, supported_formats: [] }),
      text: async () => { if (isRead) readsLanded++; return isRead ? readBody : '' },
    }
  }))
}

// One recorded EventSource per stream; a test pushes frames by hand.
class StubEventSource {
  static instances: StubEventSource[] = []
  closed = false
  onopen: (() => void) | null = null
  onmessage: ((ev: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  constructor(readonly url: string) { StubEventSource.instances.push(this) }
  close() { this.closed = true }
}
const liveStream = () => StubEventSource.instances.filter(s => !s.closed).at(-1)!
const frame = (content: string) => act(async () => { liveStream().onmessage!({ data: JSON.stringify({ content, mtime: 1 }) }) })

function Panel({ partial = true, content = capped, readOnly = true, liveWatch = false, active = true }: {
  partial?: boolean; content?: string; readOnly?: boolean; liveWatch?: boolean; active?: boolean
}) {
  return (
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <div data-testid="tab">
          <MarkdownPanel
            embedded
            active={active}
            readOnly={readOnly}
            liveWatch={liveWatch}
            initialDiffMode
            onContentChange={() => {}}
            onSave={async () => {}}
            onClose={() => {}}
            filePath="/repo/big.py"
            content={content}
            partial={partial}
          />
        </div>
      </QueryClientProvider>
    </MemoryRouter>
  )
}

/** The header's -N count, if any is drawn. */
const removedCount = () => within(screen.getByTestId('tab')).queryByText(/^-\d+$/)?.textContent ?? null

beforeEach(() => {
  vi.clearAllMocks()
  qc.clear()
  localStorage.clear()
  readBody = capped
  readHeader = 'X-Truncated'
  readsLanded = 0
  StubEventSource.instances = []
  vi.stubGlobal('EventSource', StubEventSource)
  installFetch()
  vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [] } as never)
  vi.mocked(api.fileDiff).mockResolvedValue(modified)
})
afterEach(() => { vi.restoreAllMocks() })

describe('MarkdownPanel diff of a partial (capped) read', () => {
  it.each([
    ['viewer', true],
    ['editor', false],
  ])("draws git's patch and git's counts in the %s", async (_view, readOnly) => {
    render(<Panel readOnly={readOnly} />)
    const patch = await screen.findByTestId('pierre-patch')
    expect(patch.getAttribute('data-value')).toBe(gitDiff)
    expect(screen.queryByTestId('pierre-diff')).toBeNull()
    expect(screen.queryByTestId('pierre-editor')).toBeNull()
    const tab = within(screen.getByTestId('tab'))
    await waitFor(() => expect(tab.getByText('+1')).toBeTruthy())
    expect(removedCount()).toBe('-1')
  })

  it("leaves the editor's patch view through the diff toggle, back to the editor", async () => {
    render(<Panel readOnly={false} />)
    await screen.findByTestId('pierre-patch')
    const tab = within(screen.getByTestId('tab'))
    fireEvent.click(tab.getAllByRole('button', { name: /toggle diff view/i })[0])
    await screen.findByTestId('pierre-editor')
    expect(screen.queryByTestId('pierre-patch')).toBeNull()
  })

  it.each([
    ['viewer', true],
    ['editor', false],
  ])('shows no changes and no count for a clean partial file in the %s', async (_view, readOnly) => {
    vi.mocked(api.fileDiff).mockResolvedValue(clean)
    render(<Panel readOnly={readOnly} />)
    await screen.findByText('No changes in this file')
    expect(removedCount()).toBeNull()
    expect(screen.queryByTestId('pierre-diff')).toBeNull()
    expect(screen.queryByTestId('pierre-patch')).toBeNull()
    expect(screen.queryByTestId('pierre-editor')).toBeNull()
  })

  it('opens the editor on a clean partial file when diff mode is off', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue(clean)
    render(
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <MarkdownPanel embedded active readOnly={false} initialDiffMode={false} onContentChange={() => {}} onSave={async () => {}} onClose={() => {}} filePath="/repo/big.py" content={capped} partial />
        </QueryClientProvider>
      </MemoryRouter>,
    )
    await screen.findByTestId('pierre-editor')
    expect(screen.queryByTestId('pierre-patch')).toBeNull()
  })

  it.each(['X-Truncated', 'X-Redacted'] as const)(
    "fetches git's diff once on open, and once more on a Refresh that matches the buffer (%s)",
    async header => {
      readHeader = header
      render(<Panel liveWatch />)
      await screen.findByTestId('pierre-patch')
      // The mount catch-up read lands and must not refetch the diff it raced.
      await waitFor(() => expect(readsLanded).toBe(1))
      await act(async () => {})
      expect(api.fileDiff).toHaveBeenCalledTimes(1)
      fireEvent.click(screen.getByTestId('markdown-panel-more-options'))
      fireEvent.click(await screen.findByRole('menuitem', { name: /refresh/i }))
      await waitFor(() => expect(readsLanded).toBe(2))
      await waitFor(() => expect(api.fileDiff).toHaveBeenCalledTimes(2))
      await act(async () => {})
      expect(api.fileDiff).toHaveBeenCalledTimes(2)
    },
  )

  it('follows a watch frame for an edit past the cut, whose capped text is unchanged', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue(clean)
    render(<Panel liveWatch />)
    await screen.findByText('No changes in this file')
    await waitFor(() => expect(readsLanded).toBe(1))
    // The connect-time frame repeats what the tab shows: no read, no fetch.
    await frame(capped)
    await act(async () => {})
    expect(readsLanded).toBe(1)
    expect(api.fileDiff).toHaveBeenCalledTimes(1)
    // The file then changes past the cut: the next frame carries the same
    // capped text, and the patch must follow without a Refresh.
    vi.mocked(api.fileDiff).mockResolvedValue(modified)
    await frame(capped)
    await waitFor(() => expect(screen.getByTestId('pierre-patch').getAttribute('data-value')).toBe(gitDiff))
    expect(removedCount()).toBe('-1')
  })

  it('fetches the patch again once a fetch that a second write past the cut overtook settles', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue(clean)
    render(<Panel liveWatch />)
    await screen.findByText('No changes in this file')
    await waitFor(() => expect(readsLanded).toBe(1))
    await frame(capped) // connect-time frame: quiet
    // Write 1: its diff fetch is held in flight and answers with the OLD patch.
    let releaseStale!: () => void
    vi.mocked(api.fileDiff).mockImplementationOnce(() => new Promise(r => { releaseStale = () => r(clean) }))
    await frame(capped)
    await waitFor(() => expect(api.fileDiff).toHaveBeenCalledTimes(2))
    // Write 2 lands while that fetch is still out. One fetch at a time: none
    // starts beside it, and once it settles the patch is fetched again, so the
    // stale answer never stays on screen.
    vi.mocked(api.fileDiff).mockResolvedValue(modified)
    await frame(capped)
    await waitFor(() => expect(readsLanded).toBe(3))
    await act(async () => {})
    expect(api.fileDiff).toHaveBeenCalledTimes(2)
    await act(async () => { releaseStale() })
    await waitFor(() => expect(api.fileDiff).toHaveBeenCalledTimes(3))
    await waitFor(() => expect(screen.getByTestId('pierre-patch').getAttribute('data-value')).toBe(gitDiff))
  })

  it('fetches no patch for writes while diff mode is off, and catches up when it is turned on', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue(clean)
    render(
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <div data-testid="tab">
            <MarkdownPanel embedded active liveWatch initialDiffMode={false} onContentChange={() => {}} onSave={async () => {}} onClose={() => {}} filePath="/repo/big.md" content={capped} partial />
          </div>
        </QueryClientProvider>
      </MemoryRouter>,
    )
    await waitFor(() => expect(readsLanded).toBe(1))
    await waitFor(() => expect(api.fileDiff).toHaveBeenCalledTimes(1))
    await frame(capped) // connect-time frame: quiet
    await frame(capped) // a write past the cut
    await waitFor(() => expect(readsLanded).toBe(2))
    await act(async () => {})
    expect(api.fileDiff).toHaveBeenCalledTimes(1)
    vi.mocked(api.fileDiff).mockResolvedValue(modified)
    fireEvent.click(within(screen.getByTestId('tab')).getAllByRole('button', { name: /toggle diff view/i })[0])
    await waitFor(() => expect(api.fileDiff).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByTestId('pierre-patch').getAttribute('data-value')).toBe(gitDiff))
  })

  it('refetches the patch when a hidden partial tab becomes the visible one', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue(clean)
    const { rerender } = render(<Panel liveWatch />)
    await screen.findByText('No changes in this file')
    await waitFor(() => expect(readsLanded).toBe(1))
    rerender(<Panel liveWatch active={false} />)
    // While the tab is hidden, the file changes past the cut.
    vi.mocked(api.fileDiff).mockResolvedValue(modified)
    rerender(<Panel liveWatch active />)
    await waitFor(() => expect(readsLanded).toBe(2))
    await waitFor(() => expect(screen.getByTestId('pierre-patch').getAttribute('data-value')).toBe(gitDiff))
  })

  it('keeps the editor when the diff fetch fails', async () => {
    vi.mocked(api.fileDiff).mockRejectedValue(new Error('boom'))
    render(<Panel readOnly={false} />)
    await screen.findByTestId('markdown-panel-diff-error')
    expect(screen.getByTestId('pierre-editor')).toBeTruthy()
    expect(screen.queryByTestId('pierre-patch')).toBeNull()
  })

  it("keeps the editor's patch on screen while a Refresh refetches it", async () => {
    render(<Panel readOnly={false} liveWatch />)
    await screen.findByTestId('pierre-patch')
    await waitFor(() => expect(readsLanded).toBe(1))
    let release!: () => void
    vi.mocked(api.fileDiff).mockImplementationOnce(() => new Promise(r => { release = () => r(modified) }))
    fireEvent.click(screen.getByTestId('markdown-panel-more-options'))
    fireEvent.click(await screen.findByRole('menuitem', { name: /refresh/i }))
    await waitFor(() => expect(api.fileDiff).toHaveBeenCalledTimes(2))
    // Let the in-flight state reach the render before reading it.
    await waitFor(() => expect(qc.isFetching({ queryKey: ['file-diff', '/repo/big.py'] })).toBe(1))
    await act(async () => {})
    // The refetch is still in flight: the cached patch stays drawn.
    expect(screen.getByTestId('pierre-patch').getAttribute('data-value')).toBe(gitDiff)
    expect(screen.queryByTestId('pierre-editor')).toBeNull()
    await act(async () => { release() })
    await waitFor(() => expect(screen.getByTestId('pierre-patch').getAttribute('data-value')).toBe(gitDiff))
  })

  it('keeps the body-pair diff for a whole buffer', async () => {
    const whole = head.map((l, i) => (i === 249 ? 'line 250 edited' : l)).join('\n') + '\n'
    render(<Panel partial={false} content={whole} />)
    await screen.findByTestId('pierre-diff')
    expect(screen.queryByTestId('pierre-patch')).toBeNull()
  })

  it('keeps "no changes" for a whole buffer whose text matches HEAD, even when git calls it modified', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue({ diff: '', original: headBody, status: 'modified' })
    render(<Panel partial={false} content={headBody} />)
    await screen.findByText('No changes in this file')
    expect(screen.queryByText(changeNotShown)).toBeNull()
  })
})

describe('MarkdownPanel diff of a redacted read whose only change is masked', () => {
  beforeEach(() => {
    readHeader = 'X-Redacted'
    readBody = maskedBody
    vi.mocked(api.fileDiff).mockResolvedValue(maskedChangeOnly)
  })

  it.each([
    ['viewer', true],
    ['editor', false],
  ])('says git saw a change, not "no changes", in the %s', async (_view, readOnly) => {
    render(<Panel readOnly={readOnly} content={maskedBody} />)
    await screen.findByText(changeNotShown)
    expect(screen.queryByText('No changes in this file')).toBeNull()
    expect(removedCount()).toBeNull()
    expect(screen.queryByTestId('pierre-patch')).toBeNull()
    expect(screen.queryByTestId('pierre-editor')).toBeNull()
  })

  it('says the same in full screen', async () => {
    render(<Panel content={maskedBody} />)
    await screen.findByText(changeNotShown)
    fireEvent.click(screen.getByTestId('markdown-panel-more-options'))
    fireEvent.click(await screen.findByRole('menuitem', { name: /full screen/i }))
    const dialog = await screen.findByRole('dialog')
    await within(dialog).findByText(changeNotShown)
    expect(within(dialog).queryByText('No changes in this file')).toBeNull()
  })

  it('still says "no changes" when git calls the file clean', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue({ ...maskedChangeOnly, status: 'clean' })
    render(<Panel content={maskedBody} />)
    await screen.findByText('No changes in this file')
    expect(screen.queryByText(changeNotShown)).toBeNull()
  })
})
