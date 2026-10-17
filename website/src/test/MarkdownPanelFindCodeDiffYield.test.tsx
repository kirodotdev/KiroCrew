/**
 * Who answers Cmd/Ctrl+F in the side panel's non-markdown surfaces — the
 * residual of #6383 after #6397 landed the edit-mode half.
 *
 * Before this change the capture-phase handler bailed on
 * `editing || diffMode || !isMarkdown` as one condition, before it reached its
 * own activation signal. For a read-only diff that meant the chord fell through
 * to ChatPage's bubble-phase chat-find and opened a transcript-scoped find over
 * the diff — a find bar pointed at the conversation, which is worse than no
 * find at all.
 *
 * The fix splits that bail:
 *   • edit mode still bails here, so Pierre's editor keeps the chord and
 *     #6397's `defaultPrevented` guard stops chat-find from stacking a pane;
 *   • read-only diff and read-only code preview YIELD the chord away from
 *     chat-find (capture-phase stopImmediatePropagation) WITHOUT opening a find
 *     bar — there is no in-document find for those surfaces yet (the diff
 *     renders outside the find query root and code preview is windowed behind
 *     Pierre's <Virtualizer>; a real find for either is the open design
 *     question #6383 is parked on). Not calling preventDefault() lets a native
 *     browser find bar (where one exists) still open over the file;
 *   • markdown preview is unchanged — it still opens its own find bar.
 *
 * Globals/Pierre are stubbed exactly as in MarkdownPanelFindActiveTab.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { forwardRef, useImperativeHandle } from 'react'
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react'
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

const FIND_INPUT = 'Find in document'

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter>
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  </MemoryRouter>
)

function Panel({ filePath, content, initialDiffMode }: {
  filePath: string
  content: string
  initialDiffMode?: boolean
}) {
  return (
    <div data-testid="tab">
      <MarkdownPanel
        embedded
        active
        onContentChange={() => {}}
        onSave={async () => {}}
        onClose={() => {}}
        filePath={filePath}
        content={content}
        initialDiffMode={initialDiffMode}
      />
    </div>
  )
}

const tab = () => within(screen.getByTestId('tab'))
const panelRoot = () =>
  screen.getByTestId('tab').querySelector('[data-mc-mdpanel]') as HTMLElement
const pressFind = () => fireEvent.keyDown(document, { key: 'f', ctrlKey: true })

/**
 * A bubble-phase `document` listener standing in for ChatPage's chat-find.
 * Registered AFTER MarkdownPanel's capture-phase listener (which mounts first),
 * so a capture-phase `stopImmediatePropagation` from the panel prevents it from
 * running — exactly as it prevents the real chat-find listener.
 */
function withChatFind(run: () => void): ReturnType<typeof vi.fn> {
  const chatFind = vi.fn()
  document.addEventListener('keydown', chatFind)
  try { run() } finally { document.removeEventListener('keydown', chatFind) }
  return chatFind
}

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
  vi.mocked(api.fileDiff).mockResolvedValue(
    { diff: '@@ -1 +1 @@\n-old line\n+new line\n', original: 'old line\n', status: 'ok' } as never,
  )
})

afterEach(() => {
  vi.restoreAllMocks()
  document.body.style.overflow = ''
})

describe('MarkdownPanel — Cmd+F on a read-only diff yields away from chat-find', () => {
  it('renders the diff, then does NOT open chat-find and does NOT open a find bar', async () => {
    render(<Panel filePath="/tmp/notes.md" content={'# Notes\nnew line\n'} initialDiffMode />, { wrapper })
    await waitFor(() => expect(screen.queryByTestId('pierre-diff')).toBeTruthy())

    // The user is looking at the diff: panel is the active tab and a click lands
    // inside it, so findActiveRef is true.
    fireEvent.pointerDown(panelRoot())
    const chatFind = withChatFind(pressFind)

    expect(chatFind).not.toHaveBeenCalled()           // chat-find never answered the chord
    expect(tab().queryByLabelText(FIND_INPUT)).toBeNull()  // no in-document find bar either
  })

  it('still lets chat-find answer when the last click was in the chat, not the diff', async () => {
    render(<Panel filePath="/tmp/notes.md" content={'# Notes\nnew line\n'} initialDiffMode />, { wrapper })
    await waitFor(() => expect(screen.queryByTestId('pierre-diff')).toBeTruthy())

    // Pointer in the transcript → findActiveRef flips false → the panel must not
    // claim the chord, so chat-find runs and answers it.
    fireEvent.pointerDown(document.body)
    const chatFind = withChatFind(pressFind)

    expect(chatFind).toHaveBeenCalledTimes(1)
    expect(tab().queryByLabelText(FIND_INPUT)).toBeNull()
  })
})

describe('MarkdownPanel — edit mode is left to Pierre + #6397', () => {
  it('a code file (editor by default) is not claimed by this handler', async () => {
    // A code file renders Pierre's editor; the editor binds the chord on its own
    // content element and (in production) preventDefault()s it, which #6397's
    // defaultPrevented guard uses to stop chat-find. This handler must stay out
    // of the way: it opens no find bar of its own for the editor surface.
    render(<Panel filePath="/tmp/app.py" content={'x = 1\ny = 2\n'} />, { wrapper })
    await waitFor(() => expect(screen.queryByTestId('pierre-editor')).toBeTruthy())

    fireEvent.pointerDown(panelRoot())
    pressFind()

    expect(tab().queryByLabelText(FIND_INPUT)).toBeNull()
  })
})

describe('MarkdownPanel — markdown preview find is unchanged (regression guard)', () => {
  it('still opens its own in-document find bar on the active markdown tab', async () => {
    render(<Panel filePath="/tmp/readme.md" content={'# Readme\n\nhello hello\n'} />, { wrapper })
    await waitFor(() => expect(panelRoot()).toBeTruthy())

    fireEvent.pointerDown(panelRoot())
    pressFind()

    expect(tab().getByLabelText(FIND_INPUT)).toBeTruthy()
  })
})
