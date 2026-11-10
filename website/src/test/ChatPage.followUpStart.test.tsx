/**
 * The follow-up card's Start handlers as ChatPage wires them: the separate-copy
 * (git worktree) route, the new-chat route, one-click send and the default
 * pre-fill, with each failure branch of the refused-send cleanup.
 *
 * `FollowUpCard` is stubbed as a PROP RECORDER so `onStartInWorktree` and
 * `onStartInNewChat` can be invoked directly; the card's own rendering is
 * covered by FollowUpCard.test.tsx. Split out of ChatPageW3Coverage.test.tsx,
 * whose setup this file shares.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { store as appStore } from '../store'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'
import type { PasteBlock } from '../utils/pasteTokens'
import { PREFILL_STORAGE_KEY } from '../utils/navIntent'
import { setFollowupCard, selectSendAttempts, switchSlot } from '../store/chatSlice'
import { queuedSendStash } from '../hooks/useQueuedMessageActions'
import { ApiError } from '../api/apiError'

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

// --- The follow-up card is a prop recorder; the other children render nothing --

vi.mock('../components/QueueStack', async (importOriginal) => {
  // isSystemDelivery / isNonInteractiveQueued are imported from here by
  // ChatPage to classify queued rows — keep the real ones.
  const actual = await importOriginal<typeof import('../components/QueueStack')>()
  return {
    ...actual,
    default: () => null,
    SubagentDeliveryProgress: () => null,
  }
})

interface FollowupItem { title: string; prompt: string; description?: string; branch?: string }
interface FollowupProps {
  items: FollowupItem[]
  onAddToSession: (item: FollowupItem, index: number) => void
  onStartInWorktree: (item: FollowupItem, index: number) => Promise<void>
  onStartInNewChat: (item: FollowupItem, index: number) => Promise<void>
  onSkip: (index: number) => void
}
let followupProps: FollowupProps | null = null
vi.mock('../components/FollowUpCard', () => ({
  default: (props: FollowupProps) => { followupProps = props; return null },
}))

vi.mock('../pages/chat/SidePanel', () => ({
  default: () => null,
  CHAT_PANE_MIN_W: 420,
  sidePanelFillWidth: () => false,
}))

interface InputProps {
  value: string
  onChange: (v: string) => void
  onSend: () => void
  onFileSelect?: (path: string, kind?: string, token?: string) => void
  onScreenshot?: () => void
  pendingFiles?: string[]
  uploading?: boolean
}
let inputProps: InputProps | null = null
vi.mock('../components/ChatInput', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../components/ChatInput')>()
  const React = await import('react')
  // The page hands the text over through the Composer root's draft store, not
  // a `value` prop; the stand-in reads it the way the real ChatInput does.
  const { useComposerDraftText } = await import('../chat-core/composer/Composer')
  return {
    ...actual,
    default: function ChatInputStub(rawProps: InputProps) {
      const draft = useComposerDraftText()
      const props = draft === null ? rawProps : { ...rawProps, value: draft }
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
      retainRange: vi.fn(),
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
          prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 }
            : prop === 'pendingQuestions' || prop === 'approvals' ? [] : {},
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

import ChatPage from '../pages/ChatPage'

// --- Fixtures ---------------------------------------------------------------

const SLOT = {
  key: 'chat-1', title: 'chat-1', messages: 0, running: false,
  mode: '', created: '', last_ts: '', project: '/repo/main',
}

const msg = (role: string, content: string, extra: Partial<ChatMessage> = {}): ChatMessage => ({
  role, content, cls: '', ...extra,
})

interface RenderOpts {
  chat?: Record<string, unknown>
  /** Server-side queue for the active slot. The mount fetch is authoritative —
   *  `hydrateQueuedBubbles` rebuilds the queued rows from THIS list and drops
   *  any pre-seeded ones, so a preloaded `queued` message never survives. */
  queue?: { id: string; content: string; meta?: Record<string, unknown> }[]
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
  const rendered = render(
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
  return { store, ...rendered }
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  // Module-level store shared by every host: entries would otherwise leak
  // across tests that reuse queue ids ('q1', 'q2'), making the suite
  // order-dependent.
  queuedSendStash.clear()
  followupProps = null
  inputProps = null
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

describe('ChatPage — follow-up "start in worktree"', () => {
  // The refused-send cleanup reads the module-singleton store's slot rows, so a
  // row one test seeds (another window using chat-2) must not carry into the next.
  beforeEach(() => {
    appStore.dispatch({ type: 'dashboard/sseSlots', payload: [] })
    appStore.dispatch({ type: 'chat/setAgentSwitchNotice', payload: null })
  })

  const ITEM: FollowupItem = { title: 'Add a Retry Guard!', prompt: 'add the retry guard' }

  async function renderWithFollowup(items: FollowupItem[] = [ITEM]) {
    const out = renderChatPage(
      [msg('user', 'done?', { ts: '2026-08-12T07:00:00Z' })],
      {
        chat: {
          activeSlot: 'chat-1',
          followups: { 'chat-1': { items, ts: 1000 } },
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

  const sendAccepted = () => apiSpy('sendChat').mockResolvedValue({ ok: true, json: async () => ({ ok: true }) })
  const sendRefused = () => apiSpy('sendChat').mockResolvedValue({ ok: false, status: 403, json: async () => ({ ok: false, error: 'session expired' }) })
  /** The (message, slot) of every send that went out. */
  const sends = () => apiSpy('sendChat').mock.calls.map(c => [c[0], c[1]])

  /** Whether anything switched to *key*: `switchSlot` reads its history first. */
  const switchedTo = (key: string) => apiSpy('chatSlotDetail').mock.calls.some(c => c[0] === key)

  it('creates the worktree, sends the prompt to the new session once, and keeps the user on the origin', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
    apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
    sendAccepted()
    const { store } = await renderWithFollowup()
    await act(async () => { await followupProps!.onStartInWorktree(ITEM, 0, 'send') })
    // Branch name slugified from the title under FOLLOWUP_BRANCH_RE's grammar.
    expect(apiMocks.createWorktree).toHaveBeenCalledWith('/repo/main', 'followup/add-a-retry-guard')
    // Exactly the item's prompt, exactly once, addressed to the NEW session.
    expect(sends()).toEqual([['add the retry guard', 'chat-2']])
    // Sent, not pre-filled: nothing is left for a composer to pick up.
    expect(sessionStorage.getItem(PREFILL_STORAGE_KEY)).toBeNull()
    expect(store.getState().chat.pendingInput).toBeNull()
    // Only THIS card is cleared, keyed on the ts that was rendered.
    expect(store.getState().chat.followups?.['chat-1']).toBeUndefined()
    // The run stays in the background: no switch, and the new session is listed
    // in the sidebar.
    expect(store.getState().chat.activeSlot).toBe('chat-1')
    expect(switchedTo('chat-2')).toBe(false)
    expect(store.getState().dashboard.slots.some(s => s.key === 'chat-2')).toBe(true)
    // The shell notice names where it runs and offers to open it.
    expect(store.getState().chat.agentSwitchNotice).toEqual({
      message: 'The follow-up is running in a new session on the copy at /repo/wt-retry.',
      openSlot: 'chat-2',
    })
  })

  const A: FollowupItem = { title: 'First', prompt: 'do the first thing' }
  const C: FollowupItem = { title: 'Third', prompt: 'do the third thing' }

  it('starting one of three items removes only that item, and the rest are there on return', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
    apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
    sendAccepted()
    const { store } = await renderWithFollowup([A, ITEM, C])
    await act(async () => { await followupProps!.onStartInWorktree(ITEM, 1, 'send') })
    expect(sends()).toEqual([['add the retry guard', 'chat-2']])
    expect(store.getState().chat.followups?.['chat-1']).toEqual({ items: [A, C], ts: 1000 })
    // Away on the new session the origin card is not rendered; back on the
    // origin session it renders the two remaining items.
    act(() => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'chat-2' }) })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2'))
    followupProps = null
    act(() => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'chat-1' }) })
    await waitFor(() => expect(followupProps?.items).toEqual([A, C]))
  })

  it('leaves a newer card alone when it arrived while the start was in flight', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
    let resolveCreate: (v: { key: string }) => void = () => {}
    apiSpy('createChatSlot').mockImplementation(() => new Promise(res => { resolveCreate = res }))
    sendAccepted()
    const { store } = await renderWithFollowup([A, ITEM, C])
    const pending = followupProps!.onStartInWorktree(ITEM, 1, 'send')
    const NEWER: FollowupItem[] = [{ title: 'Newer', prompt: 'newer prompt' }, ITEM]
    act(() => { store.dispatch(setFollowupCard({ slot: 'chat-1', items: NEWER, ts: 2000 })) })
    await waitFor(() => expect(apiMocks.createChatSlot).toHaveBeenCalled())
    await act(async () => { resolveCreate({ key: 'chat-2' }); await pending })
    // The completion is pinned to ts 1000, so the ts 2000 card keeps both items,
    // including the one whose content matches the started item.
    expect(store.getState().chat.followups?.['chat-1']).toEqual({ items: NEWER, ts: 2000 })
  })

  it('honours a branch name the agent supplied instead of slugifying', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-x' })
    apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
    sendAccepted()
    await renderWithFollowup()
    commitActiveSlot('chat-2')
    await act(async () => {
      await followupProps!.onStartInWorktree({ ...ITEM, branch: 'fix/explicit' }, 0)
    })
    expect(apiMocks.createWorktree).toHaveBeenCalledWith('/repo/main', 'fix/explicit')
  })

  it('falls back to a placeholder slug when the title has no usable characters', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-y' })
    apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
    sendAccepted()
    await renderWithFollowup()
    commitActiveSlot('chat-2')
    await act(async () => {
      await followupProps!.onStartInWorktree({ ...ITEM, title: '!!! ???' }, 0)
    })
    expect(apiMocks.createWorktree).toHaveBeenCalledWith('/repo/main', 'followup/suggestion')
  })

  it('refuses before creating a session when the worktree returns no path', async () => {
    apiSpy('createWorktree').mockResolvedValue({ error: 'branch already exists' })
    await renderWithFollowup()
    await expect(followupProps!.onStartInWorktree(ITEM, 0, 'send')).rejects.toThrow('branch already exists')
    // Creating the session first would leave an empty one to clean up by hand.
    expect(apiSpy('createChatSlot')).not.toHaveBeenCalled()
    expect(apiSpy('sendChat')).not.toHaveBeenCalled()
  })

  it('names the reusable worktree when the session could not be created', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
    apiSpy('createChatSlot').mockRejectedValue(new Error('slot service down'))
    await renderWithFollowup()
    await expect(followupProps!.onStartInWorktree(ITEM, 0, 'send')).rejects.toThrow(
      /copy is ready at \/repo\/wt-retry.*session could not be opened\. Press Run in a separate copy again to retry: the same copy is reused/s,
    )
    expect(apiSpy('sendChat')).not.toHaveBeenCalled()
  })

  it('fails closed rather than sending anywhere when no key came back', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
    // A fulfilled thunk with no key would otherwise leave an empty send target.
    apiSpy('createChatSlot').mockResolvedValue({})
    await renderWithFollowup()
    await expect(followupProps!.onStartInWorktree(ITEM, 0, 'send')).rejects.toThrow(/no session came back/)
    expect(apiSpy('sendChat')).not.toHaveBeenCalled()
    expect(sessionStorage.getItem(PREFILL_STORAGE_KEY)).toBeNull()
  })

  it('reports a refused send on the card, keeps the card, and stays on the origin', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
    apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
    sendRefused()
    const { store } = await renderWithFollowup()
    act(() => { inputProps!.onChange('half-typed thought') })
    // The server's reason comes first, so the person learns why it was refused.
    await expect(followupProps!.onStartInWorktree(ITEM, 0, 'send')).rejects.toThrow(
      /session expired\. The copy is ready at \/repo\/wt-retry.*prompt was not sent\. Press Run in a separate copy again to retry/s,
    )
    expect(sends()).toEqual([['add the retry guard', 'chat-2']])
    expect(store.getState().chat.activeSlot).toBe('chat-1')
    expect(store.getState().chat.followups?.['chat-1']).toBeDefined()
    // The empty session is closed, so a retry is not refused as worktree-in-use.
    await waitFor(() => expect(apiSpy('discardChatSlot')).toHaveBeenCalledWith('chat-2'))
    // The prompt went to no composer: the origin keeps only its own draft.
    expect(inputProps!.value).toBe('half-typed thought')
    expect(sessionStorage.getItem(PREFILL_STORAGE_KEY)).toBeNull()
  })

  it('adds no second full stop when the refusal reason already ends a sentence', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
    apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
    apiSpy('sendChat').mockResolvedValue({ ok: false, status: 403, json: async () => ({ ok: false, error: 'session expired!' }) })
    await renderWithFollowup()
    await expect(followupProps!.onStartInWorktree(ITEM, 0, 'send')).rejects.toThrow(/session expired! The copy is ready/)
  })

  it('keeps a refused start\'s session when another window has already used it', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
    apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
    apiSpy('sendChat').mockImplementation(async () => {
      // Another window opened the published session and started a turn in it.
      appStore.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'chat-1', messages: 1, running: false }, { key: 'chat-2', messages: 2, running: true }] })
      return { ok: false, status: 403, json: async () => ({ ok: false, error: 'session expired' }) }
    })
    const { store } = await renderWithFollowup()
    // Resolves: the kept session holds the worktree, so a retry would be refused
    // as in use. The item leaves the card and the page points at the session.
    await act(async () => { await followupProps!.onStartInWorktree(ITEM, 0, 'send') })
    expect(apiSpy('discardChatSlot')).not.toHaveBeenCalled()
    expect(store.getState().chat.followups?.['chat-1']).toBeUndefined()
    expect(store.getState().chat.activeSlot).toBe('chat-1')
    expect(await screen.findByText(/prompt was not sent, and its session is still open.*sidebar/s)).toBeInTheDocument()
  })

  it('offers no retry when a refused start\'s session will not close', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
    apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
    sendRefused()
    apiSpy('discardChatSlot').mockRejectedValueOnce(new Error('delete failed'))
    const { store } = await renderWithFollowup()
    await act(async () => { await followupProps!.onStartInWorktree(ITEM, 0, 'send') })
    expect(apiSpy('discardChatSlot')).toHaveBeenCalledWith('chat-2')
    expect(store.getState().chat.followups?.['chat-1']).toBeUndefined()
    expect(store.getState().chat.activeSlot).toBe('chat-1')
    expect(await screen.findByText(/session expired.*prompt was not sent, and its session is still open.*sidebar/s)).toBeInTheDocument()
    // The failed close raises no sidebar notice of its own, not even after its
    // recovery read: the page notice above already reports it, with what to do next.
    // The uncoded refusal's notice would land when that read settles; its
    // mocked reply resolves in microtasks, which a zero-delay task drains.
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    expect(store.getState().chat.closeRefused).toBeNull()
  })

  it('keeps the session, with no retry, when another close got to it first', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
    apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
    sendRefused()
    // A 404: another close popped the key, and its save can still restore it.
    apiSpy('discardChatSlot').mockRejectedValueOnce(new ApiError(404, 'slot not found'))
    const { store } = await renderWithFollowup()
    await act(async () => { await followupProps!.onStartInWorktree(ITEM, 0, 'send') })
    expect(store.getState().chat.followups?.['chat-1']).toBeUndefined()
    expect(await screen.findByText(/prompt was not sent, and its session is still open.*sidebar/s)).toBeInTheDocument()
  })

  it('keeps the unconfirmed prompt recallable in the new session when opening it loads an empty history', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
    apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
    // The POST never reached the server; the history read when the person opens
    // the session then works and returns its empty transcript.
    apiSpy('sendChat').mockRejectedValue(new TypeError('network reset'))
    const { store } = await renderWithFollowup()
    await act(async () => { await followupProps!.onStartInWorktree(ITEM, 0, 'send') })
    await act(async () => { await store.dispatch(switchSlot('chat-2')) })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2'))
    await waitFor(() => expect(store.getState().chat.slotLoading).toBe(false))
    // The page the server answered lacks the send, so the bubble may leave the
    // transcript; the new session's ↑ still recalls the prompt, and nothing
    // resent it.
    expect(selectSendAttempts(store.getState(), 'chat-2').map(a => a.text)).toEqual(['add the retry guard'])
    expect(sends()).toEqual([['add the retry guard', 'chat-2']])
  })

  it('treats a lost send receipt as unconfirmed, not as a refusal to retry', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
    apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
    apiSpy('sendChat').mockRejectedValue(new TypeError('network reset'))
    const { store } = await renderWithFollowup()
    // Resolves: the turn may be running, so the card must not offer a second run.
    await act(async () => { await followupProps!.onStartInWorktree(ITEM, 0, 'send') })
    expect(sends()).toEqual([['add the retry guard', 'chat-2']])
    expect(apiSpy('discardChatSlot')).not.toHaveBeenCalled()
    expect(store.getState().chat.followups?.['chat-1']).toBeUndefined()
    // The item is gone, so the notice is where to check on it: an error that
    // offers to open the session, with the user still on the origin.
    expect(store.getState().chat.activeSlot).toBe('chat-1')
    expect(switchedTo('chat-2')).toBe(false)
    const notice = store.getState().chat.agentSwitchNotice
    expect(notice).toMatchObject({ error: true, openSlot: 'chat-2' })
    expect(notice?.message).toMatch(/could not confirm the prompt reached it\. Open that session/)
  })

  it('treats a late send receipt as unconfirmed, with no resend-manually notice', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
      apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
      apiSpy('sendChat').mockImplementation((...args: unknown[]) => new Promise((_res, rej) => {
        const signal = args[3] as AbortSignal | undefined
        signal?.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError')))
      }))
      const { store } = await renderWithFollowup()
      let started: Promise<void> | undefined
      act(() => { started = followupProps!.onStartInWorktree(ITEM, 0, 'send') })
      await act(async () => { await vi.advanceTimersByTimeAsync(15_000); await started })
      expect(apiSpy('discardChatSlot')).not.toHaveBeenCalled()
      expect(store.getState().chat.followups?.['chat-1']).toBeUndefined()
      expect(store.getState().chat.agentSwitchNotice).toMatchObject({ error: true, openSlot: 'chat-2' })
      expect(store.getState().chat.agentSwitchNotice?.message).toMatch(/could not confirm the prompt reached it/)
      expect(screen.queryByText(/resend/i)).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('leaves the user where they went when they switched sessions during the worktree create', async () => {
    let release: ((v: unknown) => void) | undefined
    apiSpy('createWorktree').mockImplementation(() => new Promise(res => { release = res }))
    apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
    sendAccepted()
    await renderWithFollowup()
    let started: Promise<void> | undefined
    act(() => { started = followupProps!.onStartInWorktree(ITEM, 0, 'send') })
    await waitFor(() => expect(release).toBeDefined())
    commitActiveSlot('chat-3')
    await act(async () => { release!({ path: '/repo/wt-retry' }); await started })
    expect(appStore.getState().chat.activeSlot).toBe('chat-3')
    expect(apiSpy('chatSlotDetail').mock.calls.some(c => c[0] === 'chat-2')).toBe(false)
  })

  it('leaves the user where they went when they switched sessions during the start', async () => {
    apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
    apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
    let deliver: (() => void) | undefined
    apiSpy('sendChat').mockImplementation(() => new Promise(res => {
      deliver = () => res({ ok: true, json: async () => ({ ok: true }) })
    }))
    const { store } = await renderWithFollowup()
    let started: Promise<void> | undefined
    act(() => { started = followupProps!.onStartInWorktree(ITEM, 0, 'send') })
    await waitFor(() => expect(deliver).toBeDefined())
    // The user opens another session while the send is in flight.
    commitActiveSlot('chat-3')
    await act(async () => { deliver!(); await started })
    // The hook reads the module-singleton store, which is where the switch landed.
    expect(appStore.getState().chat.activeSlot).toBe('chat-3')
    expect(switchedTo('chat-2')).toBe(false)
    expect(store.getState().chat.followups?.['chat-1']).toBeUndefined()
    expect(store.getState().chat.agentSwitchNotice).toMatchObject({ openSlot: 'chat-2' })
  })

  describe('the new-chat route (no repository to branch)', () => {
    it('ignores a repeated start while the same card action is pending', async () => {
      apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
      let deliver: (() => void) | undefined
      apiSpy('sendChat').mockImplementation(() => new Promise(res => {
        deliver = () => res({ ok: true, json: async () => ({ ok: true }) })
      }))
      await renderWithFollowup()
      let first: Promise<void> | undefined
      act(() => { first = followupProps!.onStartInNewChat(ITEM, 0, 'send') })
      await waitFor(() => expect(deliver).toBeDefined())

      await act(async () => { await followupProps!.onStartInNewChat(ITEM, 0, 'send') })

      expect(apiSpy('createChatSlot')).toHaveBeenCalledTimes(1)
      expect(apiSpy('sendChat')).toHaveBeenCalledTimes(1)
      await act(async () => { deliver!(); await first })
    })

    it('keeps a pending start guarded when the chat page remounts', async () => {
      apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
      let deliver: (() => void) | undefined
      apiSpy('sendChat').mockImplementation(() => new Promise(res => {
        deliver = () => res({ ok: true, json: async () => ({ ok: true }) })
      }))
      const firstRender = await renderWithFollowup()
      let first: Promise<void> | undefined
      act(() => { first = followupProps!.onStartInNewChat(ITEM, 0, 'send') })
      await waitFor(() => expect(deliver).toBeDefined())

      firstRender.unmount()
      await renderWithFollowup()
      await act(async () => { await followupProps!.onStartInNewChat(ITEM, 0, 'send') })

      expect(apiSpy('createChatSlot')).toHaveBeenCalledTimes(1)
      expect(apiSpy('sendChat')).toHaveBeenCalledTimes(1)
      await act(async () => { deliver!(); await first })
    })

    it('shows a refused send on the page after the user switches sessions', async () => {
      apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
      let refuse: (() => void) | undefined
      apiSpy('sendChat').mockImplementation(() => new Promise(res => {
        refuse = () => res({ ok: false, status: 403, json: async () => ({ ok: false, error: 'session expired' }) })
      }))
      await renderWithFollowup()
      let started: Promise<void> | undefined
      act(() => { started = followupProps!.onStartInNewChat(ITEM, 0, 'send') })
      await waitFor(() => expect(refuse).toBeDefined())
      commitActiveSlot('chat-3')

      await act(async () => {
        refuse!()
        await expect(started).rejects.toThrow(/prompt was not sent/)
      })

      expect(await screen.findByText(/prompt was not sent/)).toBeInTheDocument()
    })

    it('shows a refused send in the shell banner after the user leaves the Chat route', async () => {
      apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
      let refuse: (() => void) | undefined
      apiSpy('sendChat').mockImplementation(() => new Promise(res => {
        refuse = () => res({ ok: false, status: 403, json: async () => ({ ok: false, error: 'per-person limit reached' }) })
      }))
      const { unmount } = await renderWithFollowup()
      let started: Promise<void> | undefined
      act(() => { started = followupProps!.onStartInNewChat(ITEM, 0, 'send') })
      await waitFor(() => expect(refuse).toBeDefined())
      // The person opens Settings: the Chat page, its card and its error setter unmount.
      unmount()

      await act(async () => {
        refuse!()
        await expect(started).rejects.toThrow(/prompt was not sent/)
      })

      // The shell banner every route renders carries the failure and its reason.
      const notice = appStore.getState().chat.agentSwitchNotice
      expect(notice?.error).toBe(true)
      expect(notice?.message).toMatch(/per-person limit reached.*prompt was not sent/s)
    })

    it('shows a refused send on the page after the origin card is replaced', async () => {
      apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
      let refuse: (() => void) | undefined
      apiSpy('sendChat').mockImplementation(() => new Promise(res => {
        refuse = () => res({ ok: false, status: 403, json: async () => ({ ok: false, error: 'x' }) })
      }))
      const { store } = await renderWithFollowup()
      let started: Promise<void> | undefined
      act(() => { started = followupProps!.onStartInNewChat(ITEM, 0, 'send') })
      await waitFor(() => expect(refuse).toBeDefined())
      act(() => {
        store.dispatch(setFollowupCard({
          slot: 'chat-1',
          items: [{ title: 'Newer', prompt: 'newer' }],
          ts: 2000,
        }))
      })

      await act(async () => {
        refuse!()
        await expect(started).rejects.toThrow(/prompt was not sent/)
      })

      expect(await screen.findByText(/prompt was not sent/)).toBeInTheDocument()
    })

    it('opens a chat in the origin project without git, sends the prompt once, and clears the card', async () => {
      apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
      sendAccepted()
      const { store } = await renderWithFollowup()
      await act(async () => { await followupProps!.onStartInNewChat(ITEM, 0, 'send') })
      expect(apiSpy('createWorktree')).not.toHaveBeenCalled()
      // Scoped to the spawning session's own project directory.
      expect(apiMocks.chatSlotProject).toHaveBeenCalledWith('chat-2', '/repo/main')
      expect(sends()).toEqual([['add the retry guard', 'chat-2']])
      expect(sessionStorage.getItem(PREFILL_STORAGE_KEY)).toBeNull()
      expect(store.getState().chat.followups?.['chat-1']).toBeUndefined()
      expect(store.getState().chat.activeSlot).toBe('chat-1')
      expect(switchedTo('chat-2')).toBe(false)
      expect(store.getState().chat.agentSwitchNotice).toEqual({ message: 'The follow-up is running in a new chat.', openSlot: 'chat-2' })
    })

    it('removes only the started item from a card with siblings', async () => {
      apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
      sendAccepted()
      const { store } = await renderWithFollowup([A, ITEM, C])
      await act(async () => { await followupProps!.onStartInNewChat(A, 0, 'send') })
      expect(store.getState().chat.followups?.['chat-1']).toEqual({ items: [ITEM, C], ts: 1000 })
    })

    it('reports a chat that could not be created, and keeps the card', async () => {
      apiSpy('createChatSlot').mockRejectedValue(new Error('slot service down'))
      const { store } = await renderWithFollowup()
      await expect(followupProps!.onStartInNewChat(ITEM, 0, 'send')).rejects.toThrow(/new chat could not be opened/)
      expect(apiSpy('createWorktree')).not.toHaveBeenCalled()
      expect(apiSpy('sendChat')).not.toHaveBeenCalled()
      expect(store.getState().chat.followups?.['chat-1']).toBeDefined()
    })

    it('fails closed rather than sending anywhere when no key came back', async () => {
      apiSpy('createChatSlot').mockResolvedValue({})
      await renderWithFollowup()
      await expect(followupProps!.onStartInNewChat(ITEM, 0, 'send')).rejects.toThrow(/new chat could not be opened/)
      expect(apiSpy('sendChat')).not.toHaveBeenCalled()
    })

    it('points at the sidebar with no retry when a refused new chat is kept', async () => {
      apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
      sendRefused()
      apiSpy('discardChatSlot').mockRejectedValueOnce(new Error('delete failed'))
      const { store } = await renderWithFollowup()
      await act(async () => { await followupProps!.onStartInNewChat(ITEM, 0, 'send') })
      expect(store.getState().chat.followups?.['chat-1']).toBeUndefined()
      expect(store.getState().chat.activeSlot).toBe('chat-1')
      expect(await screen.findByText(/prompt was not sent, and its session is still open.*sidebar/s)).toBeInTheDocument()
    })

    it('reports a refused send on the card and keeps it', async () => {
      apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
      sendRefused()
      const { store } = await renderWithFollowup()
      await expect(followupProps!.onStartInNewChat(ITEM, 0, 'send')).rejects.toThrow(/prompt was not sent/)
      expect(store.getState().chat.activeSlot).toBe('chat-1')
      expect(store.getState().chat.followups?.['chat-1']).toBeDefined()
    })
  })

  describe('pre-fill, the default (one-click send off)', () => {
    it('creates the worktree, hands the prompt over, and clears a card whose last item it used', async () => {
      apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
      apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
      const { store } = await renderWithFollowup()
      commitActiveSlot('chat-2')
      await act(async () => { await followupProps!.onStartInWorktree(ITEM, 0, 'prefill') })
      // The prompt travels through the prefill channel BEFORE the switch, so the
      // per-slot draft restore applies it instead of racing it. Nothing is sent.
      expect(JSON.parse(sessionStorage.getItem(PREFILL_STORAGE_KEY) || '{}')).toMatchObject({
        slotKey: 'chat-2', prompt: 'add the retry guard',
      })
      expect(sends()).toEqual([])
      // The card's only item was used, so the card is gone.
      expect(store.getState().chat.followups?.['chat-1']).toBeUndefined()
    })

    it('pre-fills when the route is given no mode at all', async () => {
      apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
      apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
      await renderWithFollowup()
      commitActiveSlot('chat-2')
      const start = followupProps!.onStartInWorktree as (item: FollowupItem, index: number) => Promise<void>
      await act(async () => { await start(ITEM, 0) })
      expect(sends()).toEqual([])
      expect(JSON.parse(sessionStorage.getItem(PREFILL_STORAGE_KEY) || '{}')).toMatchObject({ slotKey: 'chat-2' })
    })

    it('names the Start button on screen when the session could not be created', async () => {
      apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
      apiSpy('createChatSlot').mockRejectedValue(new Error('slot service down'))
      await renderWithFollowup()
      await expect(followupProps!.onStartInWorktree(ITEM, 0, 'prefill')).rejects.toThrow(
        /Press Start in a separate copy again to retry: the same copy is reused/,
      )
    })

    it('fails closed rather than prefilling the wrong session when no key came back', async () => {
      apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
      // A fulfilled thunk with no key would otherwise skip every guard below and
      // prefill whatever session happens to be on screen.
      apiSpy('createChatSlot').mockResolvedValue({})
      await renderWithFollowup()
      await expect(followupProps!.onStartInWorktree(ITEM, 0, 'prefill')).rejects.toThrow(/no session came back/)
      expect(sessionStorage.getItem(PREFILL_STORAGE_KEY)).toBeNull()
    })

    it('reports a ready worktree whose session could not be opened, and keeps the card for a retry', async () => {
      apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
      apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
      const { store } = await renderWithFollowup()
      detailFailsFor('chat-2')
      await expect(followupProps!.onStartInWorktree(ITEM, 0, 'prefill')).rejects.toThrow(
        /copy is ready at \/repo\/wt-retry.*could not be opened.*press Start again/s,
      )
      expect(store.getState().chat.followups?.['chat-1']).toBeDefined()
      expect(sends()).toEqual([])
    })

    it('refuses to prefill when the switch resolved but the session never took focus', async () => {
      apiSpy('createWorktree').mockResolvedValue({ path: '/repo/wt-retry' })
      apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
      await renderWithFollowup()
      // The switch resolves, but the committed active slot never becomes chat-2:
      // prefilling here would drop the prompt into an unrelated conversation.
      await expect(followupProps!.onStartInWorktree(ITEM, 0, 'prefill')).rejects.toThrow(/is not in focus/)
    })

    it('opens a chat in the origin project without git, hands the prompt over, and clears a one-item card', async () => {
      apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
      const { store } = await renderWithFollowup()
      commitActiveSlot('chat-2')
      await act(async () => { await followupProps!.onStartInNewChat(ITEM, 0, 'prefill') })
      expect(apiSpy('createWorktree')).not.toHaveBeenCalled()
      // Scoped to the spawning session's own project directory.
      expect(apiMocks.chatSlotProject).toHaveBeenCalledWith('chat-2', '/repo/main')
      expect(JSON.parse(sessionStorage.getItem(PREFILL_STORAGE_KEY) || '{}')).toMatchObject({
        slotKey: 'chat-2', prompt: 'add the retry guard',
      })
      expect(sends()).toEqual([])
      expect(store.getState().chat.followups?.['chat-1']).toBeUndefined()
    })

    it('points at the sidebar when the created chat could not be switched to, and keeps the card', async () => {
      apiSpy('createChatSlot').mockResolvedValue({ key: 'chat-2' })
      const { store } = await renderWithFollowup()
      detailFailsFor('chat-2')
      await expect(followupProps!.onStartInNewChat(ITEM, 0, 'prefill')).rejects.toThrow(/Switch to it in the sidebar/)
      expect(store.getState().chat.followups?.['chat-1']).toBeDefined()
    })
  })

  it('adding to the session removes only that item and leaves its siblings', async () => {
    const { store } = await renderWithFollowup([A, ITEM, C])
    act(() => { followupProps!.onAddToSession(C, 2) })
    await waitFor(() => expect(inputProps!.value).toContain('do the third thing'))
    expect(store.getState().chat.followups?.['chat-1']).toEqual({ items: [A, ITEM], ts: 1000 })
  })

  it('adding to the session merges into the composer draft and clears a one-item card', async () => {
    const { store } = await renderWithFollowup()
    act(() => { inputProps!.onChange('half-typed thought') })
    await waitFor(() => expect(inputProps!.value).toBe('half-typed thought'))
    act(() => { followupProps!.onAddToSession(ITEM, 0) })
    await waitFor(() => expect(inputProps!.value).toContain('add the retry guard'))
    // APPEND, never replace — the half-typed text must survive.
    expect(inputProps!.value).toContain('half-typed thought')
    expect(store.getState().chat.followups?.['chat-1']).toBeUndefined()
    // Pre-fill only: nothing is sent.
    expect(apiSpy('sendChat')).not.toHaveBeenCalled()
  })
})
