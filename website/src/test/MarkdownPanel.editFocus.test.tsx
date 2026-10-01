/**
 * Typing in the side panel's file editor keeps focus (#15487).
 *
 * The report: a text file opened in the side panel, switched to Edit, loses
 * focus on the first keystroke that changes the text, and only that one
 * character lands. Everything between the panel and Pierre is real here --
 * `MarkdownPanel` as the side panel's file tab mounts it (saved baseline, live
 * watch, an `onContentChange` that is a new function on every render), then
 * `ContentRenderer`, `CodeEditor` and `PierreEditorImpl`. Only Pierre itself is
 * a double, because its custom elements never upgrade under happy-dom.
 *
 * The double keeps the one Pierre behaviour this bug is about: the editable
 * document is identified by `file.cacheKey`, and a new key REBUILDS it (new
 * node, fresh selection), which drops focus. So any layer that moves the key
 * while the user types -- the seam deriving it from the echoed contents, or
 * `CodeEditor` reseeding on an echo it mistakes for an outside change --
 * shows up as a rebuilt editable, focus on a detached node, and lost
 * characters.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useState, type ReactNode } from 'react'
import { render, screen, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const pierre = vi.hoisted(() => ({
  /** Every editable node the double has mounted, in order. */
  editables: [] as HTMLTextAreaElement[],
}))

vi.mock('@pierre/diffs/edit', () => ({
  Editor: class {
    constructor(readonly options: unknown) {}
    focus() {}
    getState() { return { selections: [] } }
    setSelections() {}
    setMarkers() {}
  },
}))

vi.mock('@pierre/diffs/react', async () => {
  const { createContext } = await import('react')
  type SurfaceProps = {
    file: { name: string; contents: string; cacheKey?: string }
    editorOptions?: { onChange?: (file: { name: string; contents: string }) => void }
  }
  /** Pierre's editable document: owns its own buffer once seeded, emits each
   *  edit through `editorOptions.onChange`, and is rebuilt on a new cacheKey. */
  const Editable = ({ file, editorOptions }: SurfaceProps) => (
    <textarea
      key={file.cacheKey}
      data-testid="pierre-editable"
      aria-label={file.name}
      defaultValue={file.contents}
      ref={el => { if (el && !pierre.editables.includes(el)) pierre.editables.push(el) }}
      onChange={e => editorOptions?.onChange?.({ name: file.name, contents: e.currentTarget.value })}
    />
  )
  return {
    Virtualizer: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
    EditProvider: ({ children }: { children?: ReactNode }) => <>{children}</>,
    File: (props: SurfaceProps) => <Editable {...props} />,
    MultiFileDiff: ({ newFile, editorOptions }: { newFile: SurfaceProps['file']; editorOptions?: SurfaceProps['editorOptions'] }) => (
      <Editable file={newFile} editorOptions={editorOptions} />
    ),
    FileDiff: () => null,
    WorkerPoolContext: createContext(null),
  }
})

vi.mock('../pierre/PierreImpl', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreShell: ({ children }: { children?: ReactNode }) => <>{children}</>,
  usePierreWorkerPool: () => ({ phase: 'ready', generation: 1, pool: {} }),
  useRegisterEditorSurface: () => {},
}))

vi.mock('../api/client', () => ({
  api: {
    artifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
    artifact: vi.fn().mockResolvedValue({}),
    fileDiff: vi.fn().mockResolvedValue({ diff: '', original: '', status: 'clean' }),
    revealPath: vi.fn(),
  },
}))

const { default: MarkdownPanel } = await import('../components/MarkdownPanel')

const FILE = '/proj/COMMIT_MSG.txt'
const ORIGINAL = 'Add retry to the uploader\n'

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

/** The side panel's file tab, reduced to what it hands the panel: the tab's
 *  buffer and saved baseline, a live watch, and inline callbacks (a fresh
 *  `onContentChange` every render, as `SidePanel` passes it). */
function FileTab({ onBuffer }: { onBuffer: (c: string) => void }) {
  const [tab, setTab] = useState({ content: ORIGINAL, savedContent: ORIGINAL })
  return (
    <MarkdownPanel
      embedded
      liveWatch
      filePath={FILE}
      content={tab.content}
      savedBaseline={tab.savedContent}
      onContentChange={c => { onBuffer(c); setTab(t => ({ ...t, content: c })) }}
      onDiskContent={c => setTab({ content: c, savedContent: c })}
      onSave={async () => {}}
      onClose={() => {}}
    />
  )
}

beforeEach(() => {
  qc.clear()
  pierre.editables.length = 0
  // Every read the panel makes (watch, catch-up) answers with the file as it is
  // on disk, so nothing but the user's typing moves the buffer.
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, headers: { get: () => null }, json: async () => ({}), text: async () => ORIGINAL,
  })))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('MarkdownPanel side-panel editor keeps focus while typing (#15487)', () => {
  it('keeps the same focused editable and every typed character', async () => {
    const user = userEvent.setup()
    let buffer = ORIGINAL
    render(
      <MemoryRouter><QueryClientProvider client={qc}>
        <FileTab onBuffer={c => { buffer = c }} />
      </QueryClientProvider></MemoryRouter>,
    )
    // A .txt file opens in its preview; the user switches it to Edit.
    await user.click(screen.getAllByRole('button', { name: 'Edit' })[0])
    const editable = await screen.findByTestId('pierre-editable')

    await user.type(editable, 'fix: ')

    expect(
      pierre.editables.length,
      'the editable document was rebuilt while typing (its cacheKey moved), which drops focus',
    ).toBe(1)
    expect(screen.getByTestId('pierre-editable')).toBe(editable)
    expect(document.activeElement, 'focus left the editor').toBe(editable)
    expect(buffer).toBe(`${ORIGINAL}fix: `)
  })
})
