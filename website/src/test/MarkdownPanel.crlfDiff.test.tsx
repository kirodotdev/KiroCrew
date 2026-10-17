/**
 * Diff mode on a whole buffer whose file does not use LF line endings.
 *
 * `/api/file-diff` reads HEAD through git's text-mode output, so `original`
 * holds LF line endings only, while `/api/file-read` serves the file as
 * written. Compared as they are, every CRLF line differs from its HEAD twin.
 * The count, the no-change notice and the viewer compare HEAD with the buffer
 * read the same way. The buffer itself, which the editor holds and a save
 * writes, is never rewritten. (The editor's own live diff takes HEAD in the
 * buffer's line ending: `PierreEditorImpl.test.tsx`.)
 */
import { forwardRef, useImperativeHandle } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, within, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PierreEditorHandle } from '../pierre'

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreFilePair: ({ oldFile, newFile }: { oldFile: { contents: string } | null; newFile: { contents: string } | null }) => (
    <div data-testid="pierre-diff" data-old={oldFile?.contents ?? ''} data-new={newFile?.contents ?? ''} />
  ),
  PierrePatch: () => <div data-testid="pierre-patch" />,
  PierreEditor: forwardRef<PierreEditorHandle, { file: { contents: string } }>(
    function PierreEditorStub({ file }, ref) {
      useImperativeHandle(ref, () => ({ jumpToLine: () => {}, focus: () => {} }), [])
      return <div data-testid="pierre-editor" data-value={file.contents} />
    },
  ),
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

// HEAD: 284 lines, LF only, as `original` always arrives. The working file
// adds one comment at line 24 (git's numstat: `1 0`).
const head = Array.from({ length: 284 }, (_, i) => `const v${i + 1} = ${i + 1}`)
const edited = [...head.slice(0, 23), '// one comment', ...head.slice(23)]
const withEol = (lines: string[], eol: string) => lines.join(eol) + eol
const headBody = withEol(head, '\n')
const modified = {
  diff: 'diff --git a/game.js b/game.js\n--- a/game.js\n+++ b/game.js\n@@ -23,0 +24 @@\n+// one comment',
  original: headBody,
  status: 'modified' as const,
}
const clean = { diff: '', original: headBody, status: 'clean' as const }

function Panel({ content, readOnly }: { content: string; readOnly: boolean }) {
  return (
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <div data-testid="tab">
          <MarkdownPanel
            embedded
            active
            readOnly={readOnly}
            initialDiffMode
            onContentChange={() => {}}
            onSave={async () => {}}
            onClose={() => {}}
            filePath="/repo/game.js"
            content={content}
          />
        </div>
      </QueryClientProvider>
    </MemoryRouter>
  )
}

/** The header's +N / -N counts, or null where none is drawn. */
const addedCount = () => within(screen.getByTestId('tab')).queryByText(/^\+\d+$/)?.textContent ?? null
const removedCount = () => within(screen.getByTestId('tab')).queryByText(/^-\d+$/)?.textContent ?? null

beforeEach(() => {
  vi.clearAllMocks()
  qc.clear()
  localStorage.clear()
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200,
    headers: { get: () => null },
    json: async () => ({ enabled: false, supported_formats: [] }),
    text: async () => '',
  })))
  vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [] } as never)
  vi.mocked(api.fileDiff).mockResolvedValue(modified)
})
afterEach(() => { vi.restoreAllMocks() })

const EOLS: [string, string][] = [['CRLF', '\r\n'], ['a lone CR', '\r']]
// Every line in one ending, and the shape an LF-writing tool leaves behind in
// a CRLF file: CRLF lines around one added LF line.
const buffers: [string, string][] = [
  ...EOLS.map(([name, eol]): [string, string] => [name, withEol(edited, eol)]),
  ['CRLF with the added line in LF', withEol(edited, '\r\n').replace('// one comment\r\n', '// one comment\n')],
]

describe('MarkdownPanel diff of a whole buffer that is not LF', () => {
  it.each(buffers)('shows and counts only the added line in the viewer (%s)', async (_name, content) => {
    render(<Panel readOnly content={content} />)
    const pair = await screen.findByTestId('pierre-diff')
    expect(pair.getAttribute('data-old')).toBe(headBody)
    expect(pair.getAttribute('data-new')).toBe(withEol(edited, '\n'))
    await waitFor(() => expect(addedCount()).toBe('+1'))
    expect(removedCount()).toBeNull()
  })

  it.each(EOLS)('counts only the added line in the editor and hands it the buffer as written (%s)', async (_name, eol) => {
    render(<Panel readOnly={false} content={withEol(edited, eol)} />)
    const editor = await screen.findByTestId('pierre-editor')
    await waitFor(() => expect(addedCount()).toBe('+1'))
    expect(removedCount()).toBeNull()
    expect(editor.getAttribute('data-value')).toBe(withEol(edited, eol))
  })

  it.each(EOLS.flatMap(([name, eol]) => [['viewer', name, eol], ['editor', name, eol]]))(
    'shows no changes for an unchanged file in the %s (%s)',
    async (view, _name, eol) => {
      vi.mocked(api.fileDiff).mockResolvedValue(clean)
      render(<Panel readOnly={view === 'viewer'} content={withEol(head, eol)} />)
      await screen.findByText('No changes in this file')
      expect(screen.queryByTestId('pierre-diff')).toBeNull()
      expect(screen.queryByTestId('pierre-editor')).toBeNull()
      expect(addedCount()).toBeNull()
      expect(removedCount()).toBeNull()
    },
  )
})
