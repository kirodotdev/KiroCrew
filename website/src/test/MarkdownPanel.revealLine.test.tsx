/**
 * Tests for MarkdownPanel's reveal-forces-source-mode rule.
 *
 * The trap this locks: `ContentRenderer` dispatches on `isRichType` BEFORE it
 * looks at `editing`, so forcing source mode was not enough on its own — a
 * `data.json:42` citation set `editing = true` and still rendered `JsonViewer`,
 * Monaco never mounted, and the requested jump silently did not happen.
 *
 * The inverse matters too: an image or PDF has no text source, so a
 * `diagram.png:42` citation must NOT be dragged into an editor showing base64.
 *
 * Monaco is mocked (heavy, lazy-loaded, unrenderable in jsdom) following the
 * DiffPanel/MonacoCodeBlock precedent; what is asserted is WHICH renderer the
 * panel chose, which is exactly where the bug lived.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

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
    artifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
    fileDiff: vi.fn().mockResolvedValue({ diff: '' }),
    revealPath: vi.fn(),
  },
}))

const { default: MarkdownPanel } = await import('../components/MarkdownPanel')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter><QueryClientProvider client={qc}>{children}</QueryClientProvider></MemoryRouter>
)

beforeEach(() => { qc.clear() })

function mount(filePath: string, content: string, revealLine?: { line: number; nonce: number }) {
  return render(
    <MarkdownPanel
      embedded
      filePath={filePath}
      content={content}
      onContentChange={() => {}}
      onSave={async () => {}}
      onClose={() => {}}
      revealLine={revealLine}
    />,
    { wrapper },
  )
}

describe('MarkdownPanel — a cited line forces the renderer that has lines', () => {
  it('forces SOURCE mode for a cited markdown file, not the rendered preview', async () => {
    // Markdown opens in preview by default, and preview has no per-line element to
    // scroll to (`data-sourcepos` is per block, soft wrap breaks any line
    // correspondence), so a citation has to switch it to source. This is the case
    // the reveal genuinely changes.
    mount('/x/notes.md', '# a\n\nb\n\nc\n', { line: 3, nonce: 1 })
    expect(await screen.findByTestId('monaco')).toBeInTheDocument()
  })

  it('leaves a markdown file in preview when no line was cited', async () => {
    mount('/x/notes.md', '# a\n\nb\n')
    await waitFor(() => expect(screen.queryByTestId('monaco')).toBeNull())
  })

  it('opens a cited rich file in its own viewer and drops the line', async () => {
    // A deliberate scope line, not an oversight. `isRichType` gates the
    // source/preview toggle, the Save/Cancel row, the line-number and diff controls
    // and Cmd+S, so forcing a rich file into an editor produced a file in source
    // mode with no way back to its viewer and no visible Save for a buffer the user
    // had edited. Opening the JSON viewer without jumping still beats the inert
    // chip this replaced, and it strands nothing.
    mount('/x/data.json', '{\n  "a": 1\n}\n', { line: 2, nonce: 1 })
    await waitFor(() => expect(screen.queryByTestId('monaco')).toBeNull())
    // And no half-built source-mode chrome is exposed for it.
    expect(screen.queryByText(/view preview/i)).toBeNull()
    expect(screen.queryByText(/view source/i)).toBeNull()
  })

  it('does NOT drag an image into an editor for a cited line', async () => {
    // `content` for an image is base64; Monaco would show that instead of the
    // picture. Asserted POSITIVELY (the image renders) — "no Monaco" alone would
    // also pass if the file simply failed to render at all.
    mount('/x/diagram.png', 'iVBORw0KGgo=', { line: 42, nonce: 1 })
    await waitFor(() => expect(document.querySelector('img')).not.toBeNull())
    expect(screen.queryByTestId('monaco')).toBeNull()
  })

  it('mounts Monaco for an ordinary code file citation', async () => {
    mount('/x/mod.py', 'a = 1\nb = 2\n', { line: 2, nonce: 1 })
    expect(await screen.findByTestId('monaco')).toBeInTheDocument()
  })
})
