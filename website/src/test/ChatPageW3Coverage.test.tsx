/**
 * Coverage-directed tests for two cold halves of ChatPage.tsx that the ~37
 * existing ChatPage suites leave almost entirely unexecuted.
 *
 *  1. `renderUserContent` — the exported module-level renderer for a USER
 *     bubble. Every existing suite either mocks it away or renders assistant
 *     rows, so its whole attachment/folder/paste/knowledge decision tree is
 *     cold: the standalone-upload card path, the inline `@label` chip path, the
 *     folder chip (clickable and inert variants, plus shift-click reveal), the
 *     knowledge-context chip, and the three paste shapes (token present,
 *     token missing but content re-collapsible, neither). These are exercised
 *     directly — no ChatPage render — because the function is exported and pure
 *     apart from the two callbacks injected into it.
 *
 *  2. The handlers ChatPage hands DOWN to four children that no suite drives:
 *     `QueueStack` (cancel / interrupt / edit / reorder of a queued message),
 *     `FollowUpCard`'s `onStartInWorktree` (five distinct failure branches plus
 *     the success path), `ChatInput`'s `onScreenshot`, and `SidePanel`'s
 *     `onAddSourceToChat` / `onFileSave` / `onArtifactOpen`. Each of those four
 *     is stubbed as a PROP RECORDER so the callback can be invoked directly;
 *     the children's own rendering is covered by their own suites.
 *
 * happy-dom has no layout, so the virtualizer is stubbed to mount every item
 * (the technique ChatPageCoverage.test.tsx uses). Nothing else about the page
 * is faked: the handlers, the store wiring and the render dispatch run for real.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { i18nT } from '../i18n/t'
import { store as appStore } from '../store'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'
import type { PasteBlock } from '../utils/pasteTokens'
import { PREFILL_STORAGE_KEY } from '../utils/navIntent'

// --- Stubs whose only job is to keep the render tree small -------------------

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content?: string }) => content ?? null,
}))

// PastedChip owns hover-dwell timers and a portal; its own suite covers those.
// Stubbed to a marker so a paste assertion is about ChatPage's SPLIT decision.
vi.mock('../components/PastedChip', async () => {
  const React = await import('react')
  return {
    default: ({ block }: { block: PasteBlock }) =>
      React.createElement('span', { 'data-testid': 'paste-chip' }, `#${block.seq}`),
  }
})

vi.mock('../pages/chat', async () => {
  const React = await import('react')
  return {
    ChatFooter: () => null,
    McpInfoButton: () => null,
    PinnedPrompt: () => null,
    UserMessage: ({ content }: { content: string }) =>
      React.createElement('div', { 'data-testid': 'user-msg' }, content),
    AssistantMessage: ({ content }: { content: string }) =>
      React.createElement('div', { 'data-testid': 'assistant-msg' }, content),
  }
})

// --- Prop recorders for the four children under test ------------------------

interface QueueProps {
  messages: ChatMessage[]
  onCancel: (id: string) => void
  onInterrupt: (id: string) => void
  onEdit: (id: string, content: string) => void
  onReorder: (id: string, direction: 'next' | 'later') => void
}
let queueProps: QueueProps | null = null
vi.mock('../components/QueueStack', async (importOriginal) => {
  // isSystemDelivery / isNonInteractiveQueued are imported from here by
  // ChatPage to classify queued rows — keep the real ones.
  const actual = await importOriginal<typeof import('../components/QueueStack')>()
  return {
    ...actual,
    default: (props: QueueProps) => { queueProps = props; return null },
    SubagentDeliveryProgress: () => null,
  }
})

interface FollowupItem { title: string; prompt: string; description?: string; branch?: string }
interface FollowupProps {
  items: FollowupItem[]
  onAddToSession: (item: FollowupItem) => void
  onStartInWorktree: (item: FollowupItem) => Promise<void>
  onSkip: (index: number) => void
}
let followupProps: FollowupProps | null = null
vi.mock('../components/FollowUpCard', () => ({
  default: (props: FollowupProps) => { followupProps = props; return null },
}))

interface SidePanelProps {
  onAddSourceToChat: (text: string) => void
  onFileSave: (path: string, content: string) => Promise<void>
  onArtifactOpen: (slug: string) => Promise<void>
}
let sidePanelProps: SidePanelProps | null = null
vi.mock('../pages/chat/SidePanel', () => ({
  default: (props: SidePanelProps) => { sidePanelProps = props; return null },
  CHAT_PANE_MIN_W: 420,
  sidePanelFillWidth: () => false,
}))

interface InputProps {
  value: string
  onChange: (v: string) => void
  onScreenshot?: () => void
  pendingFiles?: string[]
  uploading?: boolean
}
let inputProps: InputProps | null = null
vi.mock('../components/ChatInput', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../components/ChatInput')>()
  const React = await import('react')
  return {
    ...actual,
    default: (props: InputProps) => {
      inputProps = props
      return React.createElement('textarea', {
        'aria-label': 'Message input',
        value: props.value,
        onChange: (e: { target: { value: string } }) => props.onChange(e.target.value),
      })
    },
  }
})

vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../components/FlyingQuote', () => ({ default: () => null }))
vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/OverlayDrawer', () => ({ default: ({ children }: { children?: ReactNode }) => children }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null, DefaultAgentRow: () => null, ManageAgentsFooter: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/SearchResultsList', () => ({ default: () => null }))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ contentWidth: 'compact' }),
  CONTENT_WIDTH: {
    compact: { messages: '900px', input: '916px' },
    comfortable: { messages: '84%', input: '85%' },
    full: { messages: '92%', input: '93%' },
  },
}))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({
  useFilteredDropdown: () => ({
    filtered: [], query: '', setQuery: vi.fn(),
    selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn(),
  }),
}))
vi.mock('../hooks/useVoiceInput', () => ({
  useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }),
  voiceInputSupported: false,
}))
vi.mock('../hooks/virtualizer/useVirtualChat', () => ({
  useVirtualChat: (opts: { items?: unknown[]; getKey?: (it: unknown, i: number) => string }) => {
    const items = opts.items ?? []
    return {
      virtualItems: items.map((data, index) => ({
        key: opts.getKey ? opts.getKey(data, index) : String(index),
        index,
        mounted: true,
        data,
      })),
      isAtBottom: true,
      getFollow: () => true,
      scrollToBottom: vi.fn(),
      mountIndex: vi.fn(() => false),
      farmIsMeasured: () => true,
      farmRecord: vi.fn(() => true),
      measureRef: () => () => {},
      topSentinelRef: { current: null },
      bottomSentinelRef: { current: null },
      offsetBefore: 0,
      offsetAfter: 0,
      totalHeight: 0,
    }
  },
}))
vi.mock('../api/pins', () => ({
  PIN_PREVIEW_INPUT_MAX_CHARS: 4096,
  pinsApi: {
    list: vi.fn().mockResolvedValue({ pins: [] }),
    create: vi.fn().mockResolvedValue({}),
    remove: vi.fn().mockResolvedValue({ ok: true }),
  },
}))

const apiMocks: Record<string, ReturnType<typeof vi.fn>> = {}
/** Seed (or fetch) the mock for one api method so a test can assert it was
 *  never called — reading it off `apiMocks` lazily would report `undefined`. */
const apiSpy = (name: string) => {
  if (!(name in apiMocks)) apiMocks[name] = vi.fn().mockResolvedValue({})
  return apiMocks[name]
}
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, prop: string) => {
      if (!(prop in apiMocks)) {
        apiMocks[prop] = vi.fn().mockResolvedValue(
          prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {},
        )
      }
      return apiMocks[prop]
    },
  }),
  fileReadUrl: (p: string) => `/api/file?path=${encodeURIComponent(p)}`,
  SEARCH_MIN_CHARS: 2,
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatPage, { renderUserContent, virtualKeyFor, messageRowKey } from '../pages/ChatPage'

// --- Fixtures ---------------------------------------------------------------

const SLOT = {
  key: 'chat-1', title: 'chat-1', messages: 0, running: false,
  mode: '', created: '', last_ts: '', project: '/repo/main',
}

const msg = (role: string, content: string, extra: Partial<ChatMessage> = {}): ChatMessage => ({
  role, content, cls: '', ...extra,
})

const openFile = vi.fn()
const openFolder = vi.fn()

/** Renders just the exported user-bubble renderer (no ChatPage). */
function renderBubble(content: string, meta?: Record<string, unknown>, withFolder = true) {
  return render(
    <div>{renderUserContent(content, meta, openFile, withFolder ? openFolder : undefined)}</div>,
  )
}

interface RenderOpts {
  chat?: Record<string, unknown>
  /** Server-side queue for the active slot. The mount fetch is authoritative —
   *  `hydrateQueuedBubbles` rebuilds the queued rows from THIS list and drops
   *  any pre-seeded ones, so a preloaded `queued` message never survives. */
  queue?: { id: string; content: string }[]
}

function renderChatPage(messages: ChatMessage[], opts: RenderOpts = {}) {
  const { chat = {}, queue = [] } = opts
  apiSpy('chatSlots').mockResolvedValue([SLOT])
  apiSpy('chatSlotDetail').mockResolvedValue({
    messages, queue, has_more: false, total: messages.length,
  })
  apiSpy('sessions').mockResolvedValue({ sessions: [], has_more: false })
  // RTK's preloadedState REPLACES a slice rather than merging, so spread the
  // reducers' own initial state — a hand-rolled literal drops keys the reducers
  // then mutate blindly.
  const base = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...base.dashboard,
      status: { platform: 'darwin' } as unknown as RootState['dashboard']['status'],
      connected: true,
      slots: [SLOT] as unknown as RootState['dashboard']['slots'],
    },
    chat: {
      ...base.chat,
      activeSlot: 'chat-1',
      ...chat,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat/chat-1']}>
            <Routes>
              {/* noUrlSync: the route→slot sync would otherwise pull activeSlot
                  back to the URL's `chat-1` the moment a handler switches the
                  page to a freshly created session. */}
              <Route path="/chat/:slug?" element={<ChatPage mode="" noUrlSync />} />
            </Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { store }
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  queueProps = null
  followupProps = null
  sidePanelProps = null
  inputProps = null
  openFile.mockClear()
  openFolder.mockClear()
  localStorage.clear()
  sessionStorage.clear()
  for (const k of Object.keys(apiMocks)) delete apiMocks[k]
  fetchMock = vi.fn().mockResolvedValue({
    ok: true, status: 200, text: () => Promise.resolve(''), json: () => Promise.resolve({}),
  })
  globalThis.fetch = fetchMock as never
  // ChatPage reads the COMMITTED active slot off the module-singleton store (not
  // the Provider's), so the singleton has to be neutral between tests or one
  // test's slot leaks into the next one's guards.
  appStore.dispatch({ type: 'chat/setActiveSlot', payload: null })
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

afterEach(() => {
  appStore.dispatch({ type: 'chat/setActiveSlot', payload: null })
  vi.clearAllTimers()
  vi.useRealTimers()
})

// ===========================================================================
// 1. renderUserContent
// ===========================================================================

describe('renderUserContent — attachments', () => {
  it('renders a bare upload as a block card and opens it on click', () => {
    renderBubble('[attached_file 1] /home/u/q3/report.pdf', { files: ['/home/u/q3/report.pdf'] })
    const card = screen.getByRole('button', {
      name: i18nT('pages.chatPage.open_file', { path: '/home/u/q3/report.pdf' }),
    })
    // The LLM-facing marker must never survive into the bubble text.
    expect(document.body.textContent).not.toContain('attached_file')
    expect(card).toHaveTextContent('report.pdf')
    fireEvent.click(card)
    expect(openFile).toHaveBeenCalledWith('/home/u/q3/report.pdf')
  })

  it('disambiguates two same-named uploads by widening the label', () => {
    renderBubble(
      '[attached_file 1] /home/u/q3/report.pdf\n[attached_file 2] /home/u/q4/report.pdf',
      { files: ['/home/u/q3/report.pdf', '/home/u/q4/report.pdf'] },
    )
    expect(screen.getByText('q3/report.pdf')).toBeInTheDocument()
    expect(screen.getByText('q4/report.pdf')).toBeInTheDocument()
  })

  it('keeps a file woven into a sentence as an inline chip, not a card', () => {
    renderBubble('please read [attached_file 1] /home/u/notes.md and summarise', {
      files: ['/home/u/notes.md'],
    })
    const chip = screen.getByRole('button', {
      name: i18nT('pages.chatPage.open_file', { path: '/home/u/notes.md' }),
    })
    expect(chip).toHaveTextContent('@notes.md')
    // Inline chips flow inside the sentence, so the surrounding prose survives.
    expect(document.body.textContent).toContain('please read')
    expect(document.body.textContent).toContain('and summarise')
    fireEvent.click(chip)
    expect(openFile).toHaveBeenCalledWith('/home/u/notes.md')
  })

  it('renders an attachment nothing in the text references as a message-level card', () => {
    // Optimistic empty-caption bubble: meta carries the file, the text has no
    // token for it at all.
    renderBubble('here you go', { files: ['/home/u/diagram.pdf'] })
    expect(
      screen.getByRole('button', {
        name: i18nT('pages.chatPage.open_file', { path: '/home/u/diagram.pdf' }),
      }),
    ).toBeInTheDocument()
  })
})

describe('renderUserContent — folder references', () => {
  const CONTENT = 'check [attached_dir 1] /repo/main/src for dead code'
  const META = { dirs: ['/repo/main/src'] }

  it('rewrites a dir marker to a clickable @label/ chip', () => {
    renderBubble(CONTENT, META)
    const chip = screen.getByRole('button', {
      name: i18nT('pages.chatPage.open_folder', { path: '/repo/main/src' }),
    })
    expect(chip).toHaveTextContent('@src/')
    expect(document.body.textContent).not.toContain('attached_dir')
    fireEvent.click(chip)
    expect(openFolder).toHaveBeenCalledWith('/repo/main/src')
  })

  it('shift-click reveals the folder in the OS file manager instead of opening the panel', () => {
    renderBubble(CONTENT, META)
    fireEvent.click(
      screen.getByRole('button', {
        name: i18nT('pages.chatPage.open_folder', { path: '/repo/main/src' }),
      }),
      { shiftKey: true },
    )
    // Shift-click routes through the shared `revealOrOpen`, which passes the
    // explicit 'reveal' action to the transport.
    expect(apiMocks.revealPath).toHaveBeenCalledWith('/repo/main/src', 'reveal')
    expect(openFolder).not.toHaveBeenCalled()
  })

  it('degrades to an inert span when no folder handler is supplied', () => {
    renderBubble(CONTENT, META, false)
    expect(screen.queryByRole('button')).toBeNull()
    // Still identifies the folder — the path lives in the tooltip.
    expect(document.querySelector('[title="/repo/main/src"]')).not.toBeNull()
  })
})

describe('renderUserContent — knowledge context chip', () => {
  const KNOWLEDGE = {
    items: 2,
    tokens: 1234,
    titles: ['Runbook', 'RFC'],
    content: [{ title: 'Runbook', text: 'restart the gateway' }],
  }

  it('summarises the injected knowledge and expands it on click', () => {
    renderBubble('why is it down?', { knowledge: KNOWLEDGE })
    const toggle = screen.getByRole('button', {
      name: i18nT('pages.chatPage.expand_knowledge_context'),
    })
    expect(document.body.textContent).toContain('1,234')
    expect(screen.queryByText('restart the gateway')).toBeNull()
    fireEvent.click(toggle)
    expect(screen.getByText('restart the gateway')).toBeInTheDocument()
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(toggle)
    expect(screen.queryByText('restart the gateway')).toBeNull()
  })

  it('renders the chip but nothing to expand when only the counts travelled', () => {
    renderBubble('why is it down?', { knowledge: { items: 1, tokens: 10, titles: ['RFC'] } })
    const toggle = screen.getByRole('button', {
      name: i18nT('pages.chatPage.expand_knowledge_context'),
    })
    fireEvent.click(toggle)
    // No `content` on the meta, so the expanded panel has nothing to show and
    // must not render an empty box... the chip itself stays.
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
  })
})

describe('renderUserContent — collapsed pastes', () => {
  const block: PasteBlock = {
    id: 'pb1', seq: 1, lines: 5, content: 'l1\nl2\nl3\nl4\nl5',
  }

  it('splits the message around a paste token and keeps the prose inline', () => {
    renderBubble('before\n[ Paste #1 · 5 lines ]\nafter', { pastes: [block] })
    expect(screen.getByTestId('paste-chip')).toHaveTextContent('#1')
    expect(document.body.textContent).toContain('before')
    expect(document.body.textContent).toContain('after')
  })

  it('re-collapses an expanded history message whose token was lost', () => {
    // The backend re-serves the EXPANDED content; only meta.pastes travels with
    // it. Without the deterministic re-collapse the raw paste would be handed
    // to the markdown renderer.
    renderBubble(`before\n${block.content}\nafter`, { pastes: [block] })
    expect(screen.getByTestId('paste-chip')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('l3')
  })

  it('falls back to a plain render when neither the token nor the content is present', () => {
    renderBubble('nothing to see', { pastes: [{ ...block, content: 'unrelated text' }] })
    expect(screen.queryByTestId('paste-chip')).toBeNull()
    expect(document.body.textContent).toContain('nothing to see')
  })

  it('renders an inline file chip in the segment beside a paste chip', () => {
    renderBubble('see [attached_file 1] /x/a.txt too\n[ Paste #1 · 5 lines ]', {
      pastes: [block],
      files: ['/x/a.txt'],
    })
    expect(screen.getByTestId('paste-chip')).toBeInTheDocument()
    const chip = screen.getByRole('button', {
      name: i18nT('pages.chatPage.open_file', { path: '/x/a.txt' }),
    })
    fireEvent.click(chip)
    expect(openFile).toHaveBeenCalledWith('/x/a.txt')
  })

  it('cards an attachment the paste message never references', () => {
    renderBubble('[ Paste #1 · 5 lines ]', { pastes: [block], files: ['/x/orphan.txt'] })
    expect(screen.getByTestId('paste-chip')).toBeInTheDocument()
    expect(
      screen.getByRole('button', {
        name: i18nT('pages.chatPage.open_file', { path: '/x/orphan.txt' }),
      }),
    ).toBeInTheDocument()
  })
})

describe('row identity helpers', () => {
  it('keys an empty turn on its index rather than crashing on a missing lead row', () => {
    expect(virtualKeyFor({ kind: 'turn', items: [], complete: true }, 7, messageRowKey))
      .toBe('turn-empty-7')
  })

  it('gives a single and the turn it leads the SAME key so a regroup never remounts', () => {
    const m = msg('assistant', 'hi', { ts: '2026-08-12T09:00:00Z' })
    const single = { kind: 'single' as const, msg: m, idx: 3 }
    expect(virtualKeyFor({ kind: 'turn', items: [single], complete: false }, 0, messageRowKey))
      .toBe(virtualKeyFor(single, 0, messageRowKey))
  })
})

// ===========================================================================
// 2. Handlers handed to children
// ===========================================================================

describe('ChatPage — queued message actions', () => {
  const TWO = [
    { id: 'q1', content: 'run the tests' },
    { id: 'q2', content: 'then deploy' },
  ]

  async function renderWithQueue(queue = TWO) {
    const out = renderChatPage([msg('user', 'first', { ts: '2026-08-12T07:00:00Z' })], { queue })
    await waitFor(() => expect(queueProps?.messages?.length).toBe(2))
    return out
  }

  it('cancelling a queued card restores its text to the composer and tells the server', async () => {
    await renderWithQueue()
    act(() => { queueProps!.onCancel('q1') })
    await waitFor(() => expect(inputProps!.value).toBe('run the tests'))
    expect(apiMocks.cancelQueuedMessage).toHaveBeenCalledWith('chat-1', 'q1')
    // Optimistically removed rather than waiting for the WS echo.
    await waitFor(() => expect(queueProps!.messages.map(m => m.meta?.queueId)).toEqual(['q2']))
  })

  it('cancelling two cards in a row keeps both drafts instead of overwriting the first', async () => {
    // The restore MERGES. Assigning was the older spelling on this surface and it
    // destroyed text: the second cancel overwrote the first card's restored draft,
    // and the first card had already been optimistically retired, so that text
    // survived nowhere. Mutation check: pass the bare `setInput` as `restoreDraft`
    // in ChatPage and this goes red while the test above still passes.
    await renderWithQueue()
    act(() => { queueProps!.onCancel('q1') })
    await waitFor(() => expect(inputProps!.value).toBe('run the tests'))
    act(() => { queueProps!.onCancel('q2') })
    await waitFor(() => expect(inputProps!.value).toContain('then deploy'))
    expect(inputProps!.value).toContain('run the tests')
    await waitFor(() => expect(queueProps!.messages).toEqual([]))
  })

  it('cancelling into a half-typed composer keeps what the user was writing', async () => {
    await renderWithQueue()
    act(() => { inputProps!.onChange('half-typed thought') })
    await waitFor(() => expect(inputProps!.value).toBe('half-typed thought'))
    act(() => { queueProps!.onCancel('q1') })
    await waitFor(() => expect(inputProps!.value).toContain('run the tests'))
    expect(inputProps!.value).toContain('half-typed thought')
  })

  it('interrupting a queued card asks the server to interrupt that entry only', async () => {
    await renderWithQueue()
    act(() => { queueProps!.onInterrupt('q2') })
    expect(apiMocks.interruptSlot).toHaveBeenCalledWith('chat-1', 'q2')
    expect(apiSpy('cancelQueuedMessage')).not.toHaveBeenCalled()
  })

  it('editing a queued card trims and commits, and refuses a blank edit', async () => {
    await renderWithQueue()
    act(() => { queueProps!.onEdit('q1', '   ') })
    expect(apiSpy('editQueuedMessage')).not.toHaveBeenCalled()
    act(() => { queueProps!.onEdit('q1', '  run the tests twice  ') })
    expect(apiMocks.editQueuedMessage).toHaveBeenCalledWith('chat-1', 'q1', 'run the tests twice')
    await waitFor(() =>
      expect(queueProps!.messages.find(m => m.meta?.queueId === 'q1')?.content)
        .toBe('run the tests twice'),
    )
  })

  it('reordering submits the FULL order so a hidden system delivery is not demoted', async () => {
    // A sub-agent delivery sits between the two visible cards. It never renders
    // as a card, but omitting it from the submitted order would let the backend
    // re-append it at the tail — silently demoting automation.
    renderChatPage([msg('user', 'first', { ts: '2026-08-12T07:00:00Z' })], {
      queue: [
        { id: 'q1', content: 'run the tests' },
        { id: 'sys1', content: '[Subagent completion event] Agent X completed ✅ done' },
        { id: 'q2', content: 'then deploy' },
      ],
    })
    await waitFor(() => expect(queueProps?.messages?.length).toBe(2))
    expect(queueProps!.messages.map(m => m.meta?.queueId)).toEqual(['q1', 'q2'])
    act(() => { queueProps!.onReorder('q1', 'later') })
    expect(apiMocks.reorderQueuedMessages).toHaveBeenCalledWith('chat-1', ['q2', 'sys1', 'q1'])
  })

  it('ignores a reorder that would run off either end of the visible stack', async () => {
    await renderWithQueue()
    act(() => { queueProps!.onReorder('q1', 'next') })
    act(() => { queueProps!.onReorder('q2', 'later') })
    act(() => { queueProps!.onReorder('nope', 'later') })
    expect(apiSpy('reorderQueuedMessages')).not.toHaveBeenCalled()
  })
})

describe('ChatPage — follow-up "start in worktree"', () => {
  const ITEM: FollowupItem = { title: 'Add a Retry Guard!', prompt: 'add the retry guard' }

  async function renderWithFollowup() {
    const out = renderChatPage(
      [msg('user', 'done?', { ts: '2026-08-12T07:00:00Z' })],
      {
        chat: {
          activeSlot: 'chat-1',
          followups: { 'chat-1': { items: [ITEM], ts: 1000 } },
        },
      },
    )
    await waitFor(() => expect(followupProps).not.toBeNull())
    return out
  }

  /** chatSlotDetail is the only api call inside `switchSlot`, so failing it for
   *  one key is how the switch is made to reject without touching the store. */
  const detailFailsFor = (badKey: string) => {
    apiMocks.chatSlotDetail.mockImplementation((key: string) =>
      key === badKey
        ? Promise.reject(new Error('detail unavailable'))
        : Promise.resolve({ messages: [], queue: [], has_more: false, total: 0 }),
    )
  }

  /** The handler compares the COMMITTED active slot (module-singleton store)
   *  against the new key to decide whether a switch is needed at all. Seeding it
   *  to the new key drives the already-in-focus branch — the one that reaches
   *  the prefill + card-clear tail. */
  const commitActiveSlot = (key: string) => {
    appStore.dispatch({ type: 'chat/setActiveSlot', payload: key })
  }

  it('creates the worktree, hands the prompt over, and clears the card', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
    apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
    const { store } = await renderWithFollowup()
    commitActiveSlot('chat-2')
    await act(async () => { await followupProps!.onStartInWorktree(ITEM) })
    // Branch name slugified from the title under FOLLOWUP_BRANCH_RE's grammar.
    expect(apiMocks.createWorktree).toHaveBeenCalledWith('/repo/main', 'followup/add-a-retry-guard')
    // The prompt travels through the prefill channel BEFORE the switch, so the
    // per-slot draft restore applies it instead of racing it.
    expect(JSON.parse(sessionStorage.getItem(PREFILL_STORAGE_KEY) || '{}')).toMatchObject({
      slotKey: 'chat-2', prompt: 'add the retry guard',
    })
    // Only THIS card is cleared, keyed on the ts that was rendered.
    expect(store.getState().chat.followups?.['chat-1']).toBeUndefined()
  })

  it('honours a branch name the agent supplied instead of slugifying', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-x' })
    apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
    await renderWithFollowup()
    commitActiveSlot('chat-2')
    await act(async () => {
      await followupProps!.onStartInWorktree({ ...ITEM, branch: 'fix/explicit' })
    })
    expect(apiMocks.createWorktree).toHaveBeenCalledWith('/repo/main', 'fix/explicit')
  })

  it('falls back to a placeholder slug when the title has no usable characters', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-y' })
    apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
    await renderWithFollowup()
    commitActiveSlot('chat-2')
    await act(async () => {
      await followupProps!.onStartInWorktree({ ...ITEM, title: '!!! ???' })
    })
    expect(apiMocks.createWorktree).toHaveBeenCalledWith('/repo/main', 'followup/suggestion')
  })

  it('refuses before creating a session when the worktree returns no path', async () => {
    apiSpy('createWorktree').mockResolvedValue({ error: 'branch already exists' })
    await renderWithFollowup()
    await expect(followupProps!.onStartInWorktree(ITEM)).rejects.toThrow('branch already exists')
    // Creating the session first would leave an empty one to clean up by hand.
    expect(apiSpy('createChatSlot')).not.toHaveBeenCalled()
  })

  it('names the reusable worktree when the session could not be created', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
    apiSpy('createChatSlot').mockRejectedValue(new Error('slot service down'))
    await renderWithFollowup()
    await expect(followupProps!.onStartInWorktree(ITEM)).rejects.toThrow(
      /Worktree created at \/repo\/wt-retry.*could not be opened and scoped/s,
    )
  })

  it('fails closed rather than prefilling the wrong session when no key came back', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
    // A fulfilled thunk with no key would otherwise skip every guard below and
    // prefill whatever session happens to be on screen.
    apiSpy('createChatSlot').mockResolvedValue({})
    await renderWithFollowup()
    await expect(followupProps!.onStartInWorktree(ITEM)).rejects.toThrow(/no session was returned/)
    expect(sessionStorage.getItem(PREFILL_STORAGE_KEY)).toBeNull()
  })

  it('reports a ready worktree whose session could not be opened', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
    apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
    await renderWithFollowup()
    detailFailsFor('chat-2')
    await expect(followupProps!.onStartInWorktree(ITEM)).rejects.toThrow(
      /Worktree ready at \/repo\/wt-retry.*could not be opened/s,
    )
  })

  it('refuses to prefill when the switch resolved but the session never took focus', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
    apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
    await renderWithFollowup()
    // The switch resolves, but the committed active slot never becomes chat-2 —
    // prefilling here would drop the prompt into an unrelated conversation.
    await expect(followupProps!.onStartInWorktree(ITEM)).rejects.toThrow(/is not in focus/)
  })

  it('adding to the session merges into the composer draft and clears the card', async () => {
    const { store } = await renderWithFollowup()
    act(() => { inputProps!.onChange('half-typed thought') })
    await waitFor(() => expect(inputProps!.value).toBe('half-typed thought'))
    act(() => { followupProps!.onAddToSession(ITEM) })
    await waitFor(() => expect(inputProps!.value).toContain('add the retry guard'))
    // APPEND, never replace — the half-typed text must survive.
    expect(inputProps!.value).toContain('half-typed thought')
    expect(store.getState().chat.followups?.['chat-1']).toBeUndefined()
  })
})

describe('ChatPage — side panel callbacks', () => {
  async function renderWithPanel() {
    const out = renderChatPage(
      [msg('user', 'look at this', { ts: '2026-08-12T07:00:00Z' })],
      { chat: { activeSlot: 'chat-1', activityOpen: true } },
    )
    await waitFor(() => expect(sidePanelProps).not.toBeNull())
    return out
  }

  it('appends a review comment below whatever is already in the composer', async () => {
    await renderWithPanel()
    act(() => { sidePanelProps!.onAddSourceToChat('nit: rename this') })
    await waitFor(() => expect(inputProps!.value).toBe('nit: rename this'))
    act(() => { sidePanelProps!.onAddSourceToChat('and this one') })
    await waitFor(() => expect(inputProps!.value).toBe('nit: rename this\n\nand this one'))
  })

  it('writes an edited file through the file-write endpoint', async () => {
    await renderWithPanel()
    await act(async () => { await sidePanelProps!.onFileSave('/repo/main/a.ts', 'next') })
    expect(fetchMock).toHaveBeenCalledWith('/api/file-write', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ path: '/repo/main/a.ts', content: 'next' }),
    }))
  })

  it('surfaces a refused write instead of reporting success', async () => {
    await renderWithPanel()
    fetchMock.mockResolvedValueOnce({ ok: false, status: 403 })
    await expect(sidePanelProps!.onFileSave('/etc/hosts', 'nope')).rejects.toThrow('Save failed: 403')
  })

  it('records an involvement breadcrumb and seeds the tab when an artifact opens', async () => {
    await renderWithPanel()
    apiSpy('artifact').mockResolvedValue({ slug: 'plan', kind: 'markdown', content: '# plan' })
    await act(async () => { await sidePanelProps!.onArtifactOpen('plan') })
    expect(apiMocks.recordArtifactReference).toHaveBeenCalledWith('plan', 'chat-1')
    expect(apiMocks.artifact).toHaveBeenCalledWith('plan')
  })

  it('still opens the tab when the artifact fetch fails', async () => {
    await renderWithPanel()
    apiSpy('artifact').mockRejectedValue(new Error('gone'))
    // The panel's own query renders the error state; the open must not throw.
    await act(async () => { await sidePanelProps!.onArtifactOpen('missing') })
    expect(apiMocks.artifact).toHaveBeenCalledWith('missing')
  })

  it('ignores an artifact open with no slug', async () => {
    await renderWithPanel()
    await act(async () => { await sidePanelProps!.onArtifactOpen('') })
    expect(apiSpy('recordArtifactReference')).not.toHaveBeenCalled()
  })
})

describe('ChatPage — composer screenshot', () => {
  it('stages the captured file on the slot that asked for it', async () => {
    renderChatPage([msg('user', 'see this', { ts: '2026-08-12T07:00:00Z' })])
    await waitFor(() => expect(inputProps).not.toBeNull())
    apiSpy('screenshot').mockResolvedValue({ path: '/tmp/shot.png' })
    await act(async () => { inputProps!.onScreenshot!() })
    await waitFor(() => expect(inputProps!.pendingFiles).toContain('/tmp/shot.png'))
    expect(inputProps!.uploading).toBe(false)
  })

  it('leaves the composer untouched when the capture is cancelled', async () => {
    renderChatPage([msg('user', 'see this', { ts: '2026-08-12T07:00:00Z' })])
    await waitFor(() => expect(inputProps).not.toBeNull())
    apiSpy('screenshot').mockRejectedValue(new Error('user cancelled'))
    await act(async () => { inputProps!.onScreenshot!() })
    await waitFor(() => expect(inputProps!.uploading).toBe(false))
    expect(inputProps!.pendingFiles).toEqual([])
  })

  it('stages nothing when the capture returns no path', async () => {
    renderChatPage([msg('user', 'see this', { ts: '2026-08-12T07:00:00Z' })])
    await waitFor(() => expect(inputProps).not.toBeNull())
    apiSpy('screenshot').mockResolvedValue({ path: '' })
    await act(async () => { inputProps!.onScreenshot!() })
    await waitFor(() => expect(inputProps!.uploading).toBe(false))
    expect(inputProps!.pendingFiles).toEqual([])
  })
})
