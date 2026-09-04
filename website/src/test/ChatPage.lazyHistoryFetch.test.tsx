/**
 * #765 — the warm-reload history round-trip.
 *
 * ChatPage used to run an unconditional
 * `useEffect(() => { dispatch(fetchHistory(false)) }, [dispatch])`, so every
 * mount (every reload, every tunnel round-trip) fetched the older-sessions
 * payload even though nothing shows it at mount: the sidebar's "Older
 * sessions" section starts collapsed (state not persisted) and self-fetches
 * on expand, and the welcome-screen "Continue a previous chat?" suggestions
 * only need the list once the user has typed a query.
 *
 * These tests pin the lazy contract:
 *  1. mount alone fetches nothing;
 *  2. typing (which arms `historyQuery` after its debounce) seeds the list
 *     exactly once;
 *  3. further typing does not re-fetch (the seed ref latches).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('../pages/chat', () => ({
  ChatFooter: () => null,
  McpInfoButton: () => null,
  UserMessage: () => null,
  AssistantMessage: () => null,
}))

// ChatInput relays the real setInput so the test can type without rendering
// the full composer (textarea sizing needs layout APIs jsdom lacks).
let typedCount = 0
vi.mock('../components/ChatInput', async () => {
  const React = await import('react')
  return {
    default: (props: { onChange: (v: string) => void }) =>
      React.createElement('button', {
        'data-testid': 'type-query',
        // A different string per click, so each click is a real input change
        // (same-value setState would skip the re-render and the debounce).
        onClick: () => props.onChange('continue that refactor ' + (++typedCount)),
      }, 'type'),
  }
})

vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../hooks/virtualizer/useVirtualChat', () => ({
  useVirtualChat: () => ({
    virtualItems: [],
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
  }),
}))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
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
          prop === 'chatSlotDetail'
            ? { messages: [], has_more: false, total: 0 }
            : prop === 'sessions'
              ? { sessions: [], has_more: false }
              : {},
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
  text: () => Promise.resolve(''),
  json: () => Promise.resolve({}),
}) as never

import ChatPage from '../pages/ChatPage'

const renderChatPage = () => {
  const slot = { key: 'chat-1', title: 'chat-1', messages: 0, running: false, mode: '', created: '', last_ts: '' }
  apiMocks.chatSlots = vi.fn().mockResolvedValue([slot])
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: false,
      slots: [slot], approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0,
      unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as never,
    chat: {
      activeSlot: 'chat-1',
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
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
  return store
}

describe('ChatPage lazy history fetch (#765)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    for (const k of Object.keys(apiMocks)) delete apiMocks[k]
  })

  it('does not fetch older-sessions history on mount', async () => {
    renderChatPage()
    // Let every mount effect (and anything they chain) settle before
    // asserting the negative: the sessions mock is created lazily by the api
    // Proxy, so its absence means fetchHistory never ran.
    await act(async () => { await new Promise(r => setTimeout(r, 400)) })
    expect(apiMocks.sessions).toBeUndefined()
  })

  it('seeds history exactly once when the user types a query, and never again', async () => {
    renderChatPage()
    await act(async () => { await Promise.resolve() })
    expect(apiMocks.sessions).toBeUndefined()

    // Typing arms historyQuery after its 300ms debounce.
    fireEvent.click(screen.getByTestId('type-query'))
    await waitFor(() => expect(apiMocks.sessions).toBeDefined())
    await waitFor(() => expect(apiMocks.sessions).toHaveBeenCalledTimes(1))

    // A second keystroke (new debounce cycle) must not re-fetch: the seed
    // latches once armed.
    fireEvent.click(screen.getByTestId('type-query'))
    await new Promise(r => setTimeout(r, 400))
    expect(apiMocks.sessions).toHaveBeenCalledTimes(1)
  })
})
