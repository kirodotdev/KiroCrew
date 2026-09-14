/**
 * Links to closed sessions navigate: the roster ChatPage hands its transcript
 * renderers is the open tabs PLUS the closed sessions already listed under
 * "Older sessions", and a transcript that names a closed session the list has
 * not loaded yet seeds that list once.
 *
 * Three contracts pinned here:
 *  1. a `state.chat.history` row (a closed session) is in the roster with its
 *     title, alongside the open slots;
 *  2. a roster miss (`SessionRosterMissCtx`) fetches history exactly once, only
 *     while the list is empty — a reader who already paged deeper is not reset
 *     to page one, and a genuinely gone key cannot keep re-fetching;
 *  3. activating a closed key RESUMES it (`resumeFromHistory`, the sidebar row's
 *     path) rather than switching to it — `switchSlot` reads a slot-detail
 *     endpoint that answers 404 for any key without a live slot.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

/** Props the stubbed transcript last received, for asserting what ChatPage passes. */
const lastAssistantProps: { sessions?: ReadonlyMap<string, string> } = {}
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { SessionRosterMissCtx } from '../lib/sessionRoster'
import chatReducer from '../store/chatSlice'

// Stub AssistantMessage: capture the roster, expose the page's roster-miss hook
// as a button so a test can play the renderer meeting an unknown key, and expose
// `onSessionOpen` for a closed and an open key so activation can be exercised.
vi.mock('../pages/chat', async () => {
  const React = await import('react')
  return {
    ChatFooter: () => null,
    McpInfoButton: () => null,
    UserMessage: () => null,
    AssistantMessage: (props: { sessions?: ReadonlyMap<string, string>; content?: string; onSessionOpen?: (key: string) => void }) => {
      lastAssistantProps.sessions = props.sessions
      const onMiss = React.useContext(SessionRosterMissCtx)
      return React.createElement('div', null,
        React.createElement('span', { 'data-testid': 'transcript' }, props.content ?? ''),
        React.createElement('button', {
          'data-testid': 'miss',
          onClick: () => onMiss?.('chat-5-1700000005'),
        }, 'miss'),
        React.createElement('button', {
          'data-testid': 'open-closed',
          onClick: () => props.onSessionOpen?.('chat-5-1700000005'),
        }, 'open closed'),
        React.createElement('button', {
          'data-testid': 'open-open',
          onClick: () => props.onSessionOpen?.('chat-2'),
        }, 'open open'),
      )
    },
  }
})

vi.mock('../components/MarkdownPanel', async () => {
  const React = await import('react')
  return { default: () => React.createElement('div', { 'data-testid': 'md-panel' }) }
})
vi.mock('../components/DiffPanel', async () => {
  const React = await import('react')
  return { default: () => React.createElement('div', { 'data-testid': 'diff-panel' }) }
})

vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
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
      farmIsMeasured: () => true,
      farmRecord: () => true,
      isAtBottom: true,
      getFollow: () => true,
      scrollToBottom: vi.fn(),
      mountIndex: vi.fn(),
      measureRef: () => () => {},
      topSentinelRef: { current: null },
      bottomSentinelRef: { current: null },
      offsetBefore: 0,
      offsetAfter: 0,
    }
  },
}))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../components/ChatInput', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', async () => {
  const React = await import('react')
  return { default: () => React.createElement('div', { 'data-testid': 'welcome' }) }
})
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/OverlayDrawer', () => ({ default: ({ children }: { children?: ReactNode }) => children }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: ({ children }: { children?: ReactNode }) => children }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ contentWidth: 'compact' }),
  CONTENT_WIDTH: { compact: { messages: '900px', input: '916px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } },
}))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))

const apiMocks: Record<string, ReturnType<typeof vi.fn>> = {}
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
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({
  ok: true, status: 200,
  text: () => Promise.resolve('file content'),
  json: () => Promise.resolve({}),
}) as never

import ChatPage from '../pages/ChatPage'

const MSG = { role: 'assistant', content: 'painted transcript', ts: '2026-06-23T20:00:00Z' }

const SLOT_A = { key: 'chat-1', title: 'one', messages: 1, running: false, mode: '', created: '', last_ts: '' }
const SLOT_B = { key: 'chat-2', title: 'two', messages: 1, running: false, mode: '', created: '', last_ts: '' }
/** A closed session: on disk, listed under "Older sessions", not an open slot. */
const CLOSED = { key: 'chat-5-1700000005', title: 'closed but on disk', messages: 3 }
/** A closed session with no title: the roster falls back to the key, as for slots. */
const CLOSED_UNTITLED = { key: 'chat-6-1700000006', messages: 1 }

const renderChatPage = (history: typeof CLOSED[]) => {
  const allSlots = [SLOT_A, SLOT_B]
  apiMocks.chatSlots = vi.fn().mockResolvedValue(allSlots)
  apiMocks.chatSlotDetail = vi.fn().mockResolvedValue({ messages: [MSG], has_more: false, total: 1 })
  apiMocks.sessions = vi.fn().mockResolvedValue({ sessions: [CLOSED], has_more: false })
  apiMocks.resumeChatSlot = vi.fn().mockResolvedValue({ ok: true, key: CLOSED.key, mode: '', messages: [MSG], total: 1, has_more: false })
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: true,
      slots: allSlots, approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0,
      unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as never,
    // The reducer's own initial state, so every per-slot map `switchSlot` and
    // `resumeFromHistory` write into exists; a hand-listed subset rejects the
    // switch with a TypeError before it reaches the network.
    chat: {
      ...chatReducer(undefined, { type: '@@INIT' }),
      activeSlot: 'chat-1', messages: [MSG], history,
    } as never,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat/chat-1']}>
            <Routes>
              <Route path="/chat/:slug?" element={<ChatPage mode="" />} />
            </Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { store }
}

const seedMessage = (store: ReturnType<typeof createTestStore>) => {
  act(() => { store.dispatch({ type: 'chat/replaceMessages', payload: [MSG] }) })
}

beforeEach(() => {
  for (const k of Object.keys(apiMocks)) delete apiMocks[k]
  delete lastAssistantProps.sessions
})

describe('chip roster lists closed sessions already loaded', () => {
  it('carries a history row with its title, beside the open slots', async () => {
    const { store } = renderChatPage([CLOSED, CLOSED_UNTITLED])
    seedMessage(store)
    await screen.findByTestId('miss')

    const roster = lastAssistantProps.sessions!
    expect(roster.get(CLOSED.key)).toBe(CLOSED.title)
    expect(roster.get(CLOSED_UNTITLED.key)).toBe(CLOSED_UNTITLED.key)
    // Positive control: widening did not displace the open tabs.
    expect(roster.get('chat-1')).toBe('one')
    expect(roster.get('chat-2')).toBe('two')
  })

  it('does not list a closed session the store has not loaded', async () => {
    // Negative control for the assertion above: the row is in the roster BECAUSE
    // it was listed, not because every well-formed key resolves.
    const { store } = renderChatPage([])
    seedMessage(store)
    await screen.findByTestId('miss')

    expect(lastAssistantProps.sessions?.has(CLOSED.key)).toBe(false)
  })
})

describe('a roster miss seeds the older-sessions list', () => {
  it('fetches once while the list is empty, and the row then joins the roster', async () => {
    const { store } = renderChatPage([])
    seedMessage(store)
    const miss = await screen.findByTestId('miss')
    // Mount alone fetched nothing: the list is lazy.
    expect(apiMocks.sessions).not.toHaveBeenCalled()

    await act(async () => { fireEvent.click(miss) })
    await waitFor(() => expect(store.getState().chat.history).toHaveLength(1))
    expect(apiMocks.sessions).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(lastAssistantProps.sessions?.get(CLOSED.key)).toBe(CLOSED.title))

    // A second miss — the same key again, or a key the seed did not bring — is
    // not a second fetch: the seed is once per page life.
    await act(async () => { fireEvent.click(miss) })
    await act(async () => { fireEvent.click(miss) })
    expect(apiMocks.sessions).toHaveBeenCalledTimes(1)
  })

  it('does not fetch when the list is already populated', async () => {
    // The seed exists to fill an EMPTY list. Refetching page one over a list
    // the reader has already paged deeper into would reset it.
    const { store } = renderChatPage([CLOSED_UNTITLED])
    seedMessage(store)
    const miss = await screen.findByTestId('miss')

    await act(async () => { fireEvent.click(miss) })
    expect(apiMocks.sessions).not.toHaveBeenCalled()
    expect(store.getState().chat.history).toHaveLength(1)
  })

  it('says so through the action-error notice when the seed fails', async () => {
    // The reader clicked a link that did nothing; a silent rejection would leave
    // them guessing whether the session is gone or the list merely did not load.
    const { store } = renderChatPage([])
    apiMocks.sessions = vi.fn().mockRejectedValue(new Error('offline'))
    seedMessage(store)
    const miss = await screen.findByTestId('miss')

    await act(async () => { fireEvent.click(miss) })

    const notice = await screen.findByTestId('action-error')
    expect(notice.textContent).toMatch(/Older sessions could not be loaded/)
    // Nothing resolved: the key is still a miss, and the roster is unchanged.
    expect(lastAssistantProps.sessions?.has(CLOSED.key)).toBe(false)
    expect(store.getState().chat.history).toHaveLength(0)
  })
})

describe('activating a closed session from a transcript link', () => {
  it('resumes it through the sidebar row\'s own path and lands on it', async () => {
    // `switchSlot` reads `GET /api/chat/slots/{key}`, which answers 404 for a
    // key the gateway holds no live slot for, so a closed session must be
    // RESUMED (POST) to be shown -- the same thunk the Older-sessions row uses.
    const { store } = renderChatPage([CLOSED])
    seedMessage(store)
    const open = await screen.findByTestId('open-closed')
    apiMocks.chatSlotDetail.mockClear()

    await act(async () => { fireEvent.click(open) })

    await waitFor(() => expect(apiMocks.resumeChatSlot).toHaveBeenCalledWith(CLOSED.key, CLOSED.title))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(CLOSED.key))
    // The row became an open tab, so it left the Older-sessions list.
    expect(store.getState().chat.history.some(h => h.key === CLOSED.key)).toBe(false)
    // And no dead slot-detail read was attempted for the closed key.
    expect(apiMocks.chatSlotDetail.mock.calls.some(c => c[0] === CLOSED.key)).toBe(false)
  })

  it('still switches to an open tab, so the closed branch is not simply always-resume', async () => {
    // Positive control: an open key must keep the slot switch, which is the
    // path that reads the slot detail; resuming an already-open slot would be
    // a redundant write.
    const { store } = renderChatPage([CLOSED])
    seedMessage(store)
    const open = await screen.findByTestId('open-open')
    apiMocks.chatSlotDetail.mockClear()

    await act(async () => { fireEvent.click(open) })

    await waitFor(() => expect(apiMocks.chatSlotDetail.mock.calls.some(c => c[0] === 'chat-2')).toBe(true))
    expect(apiMocks.resumeChatSlot).not.toHaveBeenCalled()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2'))
  })
})
