/**
 * Escape with the in-document find bar open (#28747).
 *
 * Escape is how a find bar is dismissed, so it must close the find bar and
 * leave the document open. The panel's own document-level Escape handler
 * closes the document, so it has to yield to an open find bar wherever the
 * focus is: in the find input, on one of the bar's buttons, or back in the
 * document after the reader clicked a match.
 *
 * Setup mirrors MarkdownPanelFindActiveTab.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { forwardRef, useImperativeHandle } from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
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
  PierreEditor: forwardRef<PierreEditorHandle, { file: { contents: string } }>(
    function PierreEditorStub({ file }, ref) {
      useImperativeHandle(ref, () => ({ jumpToLine: () => {}, focus: () => {} }), [])
      return <div data-testid="pierre-editor" data-value={file.contents} />
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

/** The find bar's own input, which is how we tell WHICH panel opened one. */
const FIND_INPUT = 'Find in document'

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter>
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  </MemoryRouter>
)

beforeEach(() => {
  vi.clearAllMocks()
  qc.clear()
  highlightRegistry.clear()
  localStorage.clear()
  Object.defineProperty(Element.prototype, 'scrollIntoView', {
    configurable: true, writable: true, value: vi.fn(),
  })
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, headers: { get: () => null },
    json: async () => ({ enabled: false, supported_formats: [] }),
    text: async () => '',
  })))
  vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [] } as never)
  vi.mocked(api.artifact).mockResolvedValue({ live_dirty: false, pinned: false } as never)
  vi.mocked(api.fileDiff).mockResolvedValue({ diff: '', original: '', status: 'clean' } as never)
})

afterEach(() => {
  vi.restoreAllMocks()
  document.body.style.overflow = ''
})


function Panel({ onClose }: { onClose: () => void }) {
  return (
    <MarkdownPanel
      embedded
      active
      onClose={onClose}
      onContentChange={() => {}}
      onSave={async () => {}}
      filePath="/tmp/notes.md"
      content={'# Notes\n\nalpha beta alpha\n'}
    />
  )
}

const panelRoot = () => document.querySelector('[data-mc-mdpanel]') as HTMLElement

async function openFind(onClose: () => void) {
  render(<Panel onClose={onClose} />, { wrapper })
  await waitFor(() => expect(panelRoot()).toBeTruthy())
  fireEvent.pointerDown(panelRoot())
  fireEvent.keyDown(document, { key: 'f', ctrlKey: true })
  const input = screen.getByLabelText(FIND_INPUT) as HTMLInputElement
  fireEvent.change(input, { target: { value: 'alpha' } })
  return input
}

describe('MarkdownPanel — Escape with the find bar open', () => {
  it('closes only the find bar when Escape is pressed in the find input', async () => {
    const onClose = vi.fn()
    const input = await openFind(onClose)
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(screen.queryByLabelText(FIND_INPUT)).toBeNull()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('closes only the find bar when the focus has left the find input', async () => {
    const onClose = vi.fn()
    await openFind(onClose)
    // The reader clicked a match arrow, then back into the document: the key
    // no longer starts in the find input.
    fireEvent.click(screen.getByRole('button', { name: 'Next match' }))
    fireEvent.pointerDown(panelRoot())
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(screen.queryByLabelText(FIND_INPUT)).toBeNull()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('keeps the document open while one held Escape auto-repeats past the find bar', async () => {
    const onClose = vi.fn()
    await openFind(onClose)
    fireEvent.keyDown(document.body, { key: 'Escape' })
    fireEvent.keyDown(document.body, { key: 'Escape', repeat: true })
    fireEvent.keyDown(document.body, { key: 'Escape', repeat: true })
    expect(screen.queryByLabelText(FIND_INPUT)).toBeNull()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('closes the document on the next Escape, once the find bar is gone', async () => {
    const onClose = vi.fn()
    await openFind(onClose)
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.keyDown(document.body, { key: 'Escape' })
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })
})

// Lives here rather than in MarkdownPanelCoverage.test.tsx, which sits at the
// 2,000-line file cap; this file already mounts the find bar.
describe('MarkdownPanel — find bar case toggle', () => {
  it('says whether case-matching is on', async () => {
    await openFind(vi.fn())
    const toggle = screen.getByRole('button', { name: 'Case sensitive' })
    expect(toggle).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-pressed', 'true')
  })
})
