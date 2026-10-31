/**
 * Diff view on a code file whose live diff is over the inline budget (#25237).
 *
 * A code file opens in the editor, and diff mode there draws the editor's
 * live diff. Over the budget the editor drops that diff; it says so through
 * `onLiveDiffDegraded`, and with a clean buffer the panel shows its diff view
 * instead, which starts the worker diff on its own only on the visible tab.
 *
 * The editor and the pair are recorded stand-ins: the editor reports the
 * verdict the test sets in `fixture.degraded` (the budget itself is
 * `PierreEditorImpl`'s, pinned in its own test), and it re-reports whenever
 * its `diffBase` changes, as the real one does when the baseline lands.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, within, waitFor, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const fixture = vi.hoisted(() => ({ degraded: true }))

vi.mock('../pierre', async importOriginal => {
  const { forwardRef, useLayoutEffect } = await import('react')
  return {
    ...(await importOriginal<Record<string, unknown>>()),
    PierreFilePair: (props: { autoLineByLine?: boolean; newFile?: { contents: string } | null }) => (
      <div data-testid="pierre-diff" data-auto={String(!!props.autoLineByLine)} data-new={props.newFile?.contents ?? ''} />
    ),
    PierrePatch: () => <div data-testid="pierre-patch" />,
    // CodeEditor hands the editor a ref, so the stand-in accepts one.
    PierreEditor: forwardRef(function PierreEditorStandIn(props: {
      diffBase?: string | null
      onChange: (v: string) => void
      onLiveDiffDegraded?: (degraded: boolean) => void
    }, _ref) {
      useLayoutEffect(() => {
        if (props.diffBase !== undefined) props.onLiveDiffDegraded?.(fixture.degraded)
        // eslint-disable-next-line react-hooks/exhaustive-deps -- re-reports on a new baseline only, like the real editor
      }, [props.diffBase])
      return (
        <div data-testid="pierre-editor">
          <button type="button" onClick={() => props.onChange('typed by the reader\n')}>type</button>
        </div>
      )
    }),
  }
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
const { default: MarkdownPanel } = await import('../components/MarkdownPanel')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const head = 'const a = 1\nconst b = 2\n'
const content = 'const a = 1\nconst b = 3\n'
const modified = { diff: '@@ -2 +2 @@\n-const b = 2\n+const b = 3\n', original: head, status: 'modified' as const }

function Panel({ filePath = '/repo/big.ts', active = true }: { filePath?: string; active?: boolean }) {
  return (
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <div data-testid="tab">
          <MarkdownPanel
            embedded
            active={active}
            initialDiffMode
            onContentChange={() => {}}
            onSave={async () => {}}
            onClose={() => {}}
            filePath={filePath}
            content={content}
          />
        </div>
      </QueryClientProvider>
    </MemoryRouter>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  qc.clear()
  localStorage.clear()
  fixture.degraded = true
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200,
    headers: { get: () => null },
    json: async () => ({ enabled: false, supported_formats: [] }),
    text: async () => '',
  })))
  vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [] } as never)
  vi.mocked(api.fileDiff).mockResolvedValue(modified)
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('MarkdownPanel diff view of an over-budget code file', () => {
  it('T1: shows the diff view, auto-starting, in place of the editor that dropped its diff', async () => {
    render(<Panel />)
    const diff = await screen.findByTestId('pierre-diff')
    expect(diff).toHaveAttribute('data-auto', 'true')
    expect(diff).toHaveAttribute('data-new', content)
    expect(screen.queryByTestId('pierre-editor')).toBeNull()
  })

  it('T1b: does the same in full screen', async () => {
    render(<Panel />)
    await screen.findByTestId('pierre-diff')
    fireEvent.click(screen.getByTestId('markdown-panel-more-options'))
    fireEvent.click(await screen.findByRole('menuitem', { name: /full screen/i }))
    const dialog = await screen.findByRole('dialog')
    const diff = await within(dialog).findByTestId('pierre-diff')
    expect(diff).toHaveAttribute('data-auto', 'true')
    expect(within(dialog).queryByTestId('pierre-editor')).toBeNull()
  })

  it('T1e: auto-starts only while the tab is the visible one', async () => {
    const { rerender } = render(<Panel active={false} />)
    expect(await screen.findByTestId('pierre-diff')).toHaveAttribute('data-auto', 'false')
    rerender(<Panel active />)
    expect(screen.getByTestId('pierre-diff')).toHaveAttribute('data-auto', 'true')
  })

  it("T1f: forgets one file's verdict when the tab moves to another file", async () => {
    const { rerender } = render(<Panel />)
    await screen.findByTestId('pierre-diff')
    qc.setQueryData(['file-diff', '/repo/small.ts'], modified)
    fixture.degraded = false
    rerender(<Panel filePath="/repo/small.ts" />)
    expect(await screen.findByTestId('pierre-editor')).toBeInTheDocument()
    expect(screen.queryByTestId('pierre-diff')).toBeNull()
  })

  it('G1: keeps the live-diff editor when the editor kept its diff', async () => {
    fixture.degraded = false
    render(<Panel />)
    await screen.findByTestId('pierre-editor')
    await waitFor(() => expect(api.fileDiff).toHaveBeenCalled())
    await act(async () => {})
    expect(screen.getByTestId('pierre-editor')).toBeInTheDocument()
    expect(screen.queryByTestId('pierre-diff')).toBeNull()
  })

  it('G2: keeps the editor while the buffer has unsaved edits', async () => {
    fixture.degraded = false
    render(<Panel />)
    fireEvent.click(await screen.findByRole('button', { name: 'type' }))
    // A new report on the dirty buffer: diff view off and on remounts the
    // editor, which reports again.
    fixture.degraded = true
    const toggle = () => fireEvent.click(within(screen.getByTestId('tab')).getAllByRole('button', { name: /toggle diff view/i })[0])
    toggle()
    toggle()
    expect(await screen.findByTestId('pierre-editor')).toBeInTheDocument()
    await act(async () => {})
    expect(screen.queryByTestId('pierre-diff')).toBeNull()
  })

  it('G3: keeps the editor until the baseline lands, then swaps', async () => {
    let release!: () => void
    vi.mocked(api.fileDiff).mockImplementationOnce(() => new Promise(r => { release = () => r(modified) }))
    render(<Panel />)
    expect(await screen.findByTestId('pierre-editor')).toBeInTheDocument()
    expect(screen.queryByTestId('pierre-diff')).toBeNull()
    await act(async () => { release() })
    expect(await screen.findByTestId('pierre-diff')).toHaveAttribute('data-auto', 'true')
    expect(screen.queryByTestId('pierre-editor')).toBeNull()
  })

  it('T1g: diff view off and on asks the editor again, so a shrunk file gets its editor back', async () => {
    render(<Panel />)
    await screen.findByTestId('pierre-diff')
    // The file now fits the budget; the old "dropped" verdict must not keep
    // the editor from mounting to say so.
    fixture.degraded = false
    const toggle = () => fireEvent.click(within(screen.getByTestId('tab')).getAllByRole('button', { name: /toggle diff view/i })[0])
    toggle()
    toggle()
    expect(await screen.findByTestId('pierre-editor')).toBeInTheDocument()
    await act(async () => {})
    expect(screen.queryByTestId('pierre-diff')).toBeNull()
  })
})
