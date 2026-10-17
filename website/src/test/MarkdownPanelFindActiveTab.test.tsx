/**
 * Who owns a document-level key when several file tabs are mounted at once.
 *
 * `SidePanel` keeps every document tab mounted and merely hides the inactive
 * ones, so N open files means N live `MarkdownPanel` instances, each binding
 * `document` keydown twice: once capture-phase for the find chord, once for
 * Escape and Cmd/Ctrl+S. Every one of those bindings must belong to the tab the
 * user can actually see.
 *
 * For the find chord the old failure was silent: whichever panel claimed it
 * called `stopImmediatePropagation`, so a hidden panel swallowed the key
 * outright — no find bar anywhere and no chat-find fallback either. For Escape
 * the failure was worse than silent, since a hidden panel could close a
 * document that was never on screen.
 *
 * `Highlight` / `CSS.highlights` are stubbed BEFORE the dynamic import because
 * MarkdownPanel captures both into module-level constants at load time. Pierre
 * is stubbed because markdown preview never mounts it and the real module
 * pulls in Shiki.
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

/** The find bar's own input, which is how we tell WHICH panel opened one. */
const FIND_INPUT = 'Find in document'

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter>
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  </MemoryRouter>
)

/**
 * Two file tabs, mounted in the order `SidePanel` mounts them and hidden the
 * way it hides them. Tab A is mounted FIRST, so it registers its document
 * listener first — which is exactly the panel that used to win the chord
 * regardless of which tab the user was looking at.
 */
function TwoTabs({ activeId, onCloseA, onCloseB }: {
  activeId: 'a' | 'b'
  onCloseA?: () => void
  onCloseB?: () => void
}) {
  const common = {
    embedded: true as const,
    onContentChange: () => {},
    onSave: async () => {},
  }
  return (
    <>
      <div data-testid="tab-a" style={{ display: activeId === 'a' ? 'block' : 'none' }}>
        <MarkdownPanel
          {...common}
          onClose={onCloseA ?? (() => {})}
          active={activeId === 'a'}
          filePath="/tmp/a.md"
          content={'# Alpha\n\nalpha alpha alpha\n'}
        />
      </div>
      <div data-testid="tab-b" style={{ display: activeId === 'b' ? 'block' : 'none' }}>
        <MarkdownPanel
          {...common}
          onClose={onCloseB ?? (() => {})}
          active={activeId === 'b'}
          filePath="/tmp/b.md"
          content={'# Bravo\n\nbravo bravo bravo\n'}
        />
      </div>
    </>
  )
}

const tab = (id: 'a' | 'b') => within(screen.getByTestId(`tab-${id}`))
/** The panel's own root inside a tab. */
const panelRoot = (id: 'a' | 'b') =>
  screen.getByTestId(`tab-${id}`).querySelector('[data-mc-mdpanel]') as HTMLElement

const pressFind = () => fireEvent.keyDown(document, { key: 'f', ctrlKey: true })

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

describe('MarkdownPanel — Cmd+F belongs to the visible file tab', () => {
  it('opens find in the visible tab, not in the first-mounted hidden one', async () => {
    render(<TwoTabs activeId="b" />, { wrapper })
    await waitFor(() => expect(panelRoot('b')).toBeTruthy())

    fireEvent.pointerDown(panelRoot('b'))
    pressFind()

    expect(tab('b').getByLabelText(FIND_INPUT)).toBeTruthy()
    expect(tab('a').queryByLabelText(FIND_INPUT)).toBeNull()
  })

  it('leaves the chord to chat-find when the last pointer-down was outside every panel', async () => {
    render(<TwoTabs activeId="b" />, { wrapper })
    await waitFor(() => expect(panelRoot('b')).toBeTruthy())

    // A click in the chat transcript, then the chord: no panel may claim it, and
    // nothing may stop it — ChatPage listens on the bubble phase.
    fireEvent.pointerDown(document.body)
    const chatFind = vi.fn()
    document.addEventListener('keydown', chatFind)
    try {
      pressFind()
    } finally {
      document.removeEventListener('keydown', chatFind)
    }

    expect(tab('a').queryByLabelText(FIND_INPUT)).toBeNull()
    expect(tab('b').queryByLabelText(FIND_INPUT)).toBeNull()
    expect(chatFind).toHaveBeenCalledTimes(1)
  })

  it('hands the find region to the newly visible tab after a switch', async () => {
    const { rerender } = render(<TwoTabs activeId="a" />, { wrapper })
    await waitFor(() => expect(panelRoot('a')).toBeTruthy())

    // The user reads tab A, clicks in it, then switches to tab B.
    fireEvent.pointerDown(panelRoot('a'))
    rerender(<TwoTabs activeId="b" />)
    pressFind()

    expect(tab('b').getByLabelText(FIND_INPUT)).toBeTruthy()
    expect(tab('a').queryByLabelText(FIND_INPUT)).toBeNull()
  })

  it('closes an open find bar when its tab is backgrounded', async () => {
    const { rerender } = render(<TwoTabs activeId="a" />, { wrapper })
    await waitFor(() => expect(panelRoot('a')).toBeTruthy())

    fireEvent.pointerDown(panelRoot('a'))
    pressFind()
    expect(tab('a').getByLabelText(FIND_INPUT)).toBeTruthy()

    rerender(<TwoTabs activeId="b" />)
    expect(tab('a').queryByLabelText(FIND_INPUT)).toBeNull()
  })
})

/**
 * The find chord is not the only document-level key these panels bind. The same
 * handler that owns Escape also owns Cmd/Ctrl+S, and both ride the one `active`
 * early-return — so a hidden tab must answer neither.
 */
describe('MarkdownPanel — Escape belongs to the visible file tab', () => {
  it('closes only the visible tab, never a hidden sibling', async () => {
    const onCloseA = vi.fn()
    const onCloseB = vi.fn()
    render(<TwoTabs activeId="b" onCloseA={onCloseA} onCloseB={onCloseB} />, { wrapper })
    await waitFor(() => expect(panelRoot('b')).toBeTruthy())

    fireEvent.keyDown(document, { key: 'Escape' })

    // Both panels are mounted and both listen on `document`; only the one the
    // user can see may act. Closing tab A here would discard a document that
    // was never on screen.
    expect(onCloseB).toHaveBeenCalledTimes(1)
    expect(onCloseA).not.toHaveBeenCalled()
  })

  it('follows the switch, so the newly visible tab is the one Escape closes', async () => {
    const onCloseA = vi.fn()
    const onCloseB = vi.fn()
    const { rerender } = render(
      <TwoTabs activeId="b" onCloseA={onCloseA} onCloseB={onCloseB} />, { wrapper })
    await waitFor(() => expect(panelRoot('b')).toBeTruthy())

    rerender(<TwoTabs activeId="a" onCloseA={onCloseA} onCloseB={onCloseB} />)
    fireEvent.keyDown(document, { key: 'Escape' })

    expect(onCloseA).toHaveBeenCalledTimes(1)
    expect(onCloseB).not.toHaveBeenCalled()
  })
})
