/**
 * A narrow viewport alone must not override the saved send key.
 *
 * ChatPage used to pass `isMobile ? 'ctrl-enter' : chatConfig.sendOnEnter`,
 * and `isMobile` is width-only (`max-width: 767px`). So a narrow desktop pane
 * (split window, high zoom) silently dropped the saved "Enter sends" choice and
 * Enter inserted a newline. Only a narrow TOUCH device (a phone) forces
 * Ctrl+Enter; a wide touch tablet keeps the saved choice, as before.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'
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

// Capture the send mode ChatPage hands the composer.
const seen: string[] = []
vi.mock('../components/ChatInput', () => ({
  default: (props: { sendOnEnter?: string }) => { seen.push(String(props.sendOnEnter)); return null },
}))

vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../hooks/virtualizer/useVirtualChat', () => ({
  useVirtualChat: () => ({
    virtualItems: [], farmIsMeasured: () => true, farmRecord: () => true, isAtBottom: true,
    getFollow: () => true, scrollToBottom: vi.fn(), mountIndex: vi.fn(), measureRef: () => () => {},
    topSentinelRef: { current: null }, bottomSentinelRef: { current: null }, offsetBefore: 0, offsetAfter: 0,
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
// The user's saved choice: "Enter sends".
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ contentWidth: 'compact', sendOnEnter: 'enter' }),
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
              : prop === 'pendingQuestions' || prop === 'approvals' ? [] : {},
        )
      }
      return apiMocks[prop]
    },
  }),
  fileReadUrl: (p: string) => `/api/file?path=${encodeURIComponent(p)}`,
}))

// Viewport width and pointer kind are the variables under test.
let narrow = true
let touch = false
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    // A getter: useIsMobile caches the list object, so `matches` must read live.
    get matches() { return (narrow && q.includes('max-width')) || (touch && (q.includes('pointer: coarse') || q.includes('hover: none'))) },
    media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({
  ok: true, status: 200, text: () => Promise.resolve(''), json: () => Promise.resolve({}),
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
}

describe('ChatPage send key by viewport and pointer', () => {
  beforeEach(() => {
    seen.length = 0
    narrow = true
    for (const k of Object.keys(apiMocks)) delete apiMocks[k]
  })

  it('keeps the saved "Enter sends" choice on a narrow desktop pane', async () => {
    touch = false
    renderChatPage()
    await waitFor(() => expect(seen.length).toBeGreaterThan(0))
    expect(seen.at(-1)).toBe('enter')
  })

  it('uses Ctrl+Enter on a touch device', async () => {
    touch = true
    renderChatPage()
    await waitFor(() => expect(seen.length).toBeGreaterThan(0))
    expect(seen.at(-1)).toBe('ctrl-enter')
  })

  it('keeps the saved choice on a wide touch tablet', async () => {
    narrow = false
    touch = true
    renderChatPage()
    await waitFor(() => expect(seen.length).toBeGreaterThan(0))
    expect(seen.at(-1)).toBe('enter')
  })
})
