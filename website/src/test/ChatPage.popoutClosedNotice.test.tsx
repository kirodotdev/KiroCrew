/**
 * A popout's closed-session notice. Split out of ChatPage.sid.test.tsx to keep
 * that file under the line cap; it shares the same ChatPage render scaffold.
 *
 * When another window closes or deletes a popout's own session, the popout
 * shows the notice in place of the plain empty state. Only the server's
 * `removed` patch raises it: a frame or list read that merely omits the chat
 * does not, so a restore or reconnect cannot raise a false notice.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route, useLocation, useSearchParams } from 'react-router-dom'
import { createTestStore } from './helpers'
import { deleteSlot } from '../store/chatSlice'
import { fetchSlots, sseConnected, sseSlotPatch, sseSlots } from '../store/dashboardSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

/** Deep-partial preloaded state for createTestStore — test fixtures intentionally
 *  omit fields the reducer fills from initialState. */
type PreloadState = {
  dashboard?: Partial<RootState['dashboard']>
  chat?: Partial<RootState['chat']>
}

// --- Stub child components (same as ChatPage.sid test) ---
vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../components/ChatInput', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/OverlayDrawer', () => ({ default: () => null }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat', () => ({ ChatFooter: () => null, AssistantMessage: () => null, McpInfoButton: () => null, UserMessage: () => null, CronAckBar: () => null, NotificationItem: () => null, PinnedPrompt: () => null }))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../pages/chat/ChatSettings', () => ({ loadChatConfig: () => ({ contentWidth: 'compact' }), CONTENT_WIDTH: { compact: { messages: '900px', input: '916px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } } }))

// --- Stub hooks ---
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))

// --- Stub API ---
vi.mock('../api/client', () => ({
  api: Object.fromEntries(
    ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
     'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
     'chatSlotWorkspace', 'models', 'planFromChat', 'renameSlot',
     'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
     'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
     'notifications', 'status', 'generateTitle', 'kirocrewConfig', 'agentResolvedModel',
     'chatSlotsUnrestored', 'sessionsNamed'].map(k => [k, vi.fn().mockResolvedValue(
      k === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {}
    )])
  ),
}))

// --- Browser APIs ---
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

import ChatPage from '../pages/ChatPage'
import { i18nT } from '../i18n/t'
import { api } from '../api/client'
import { __setNavigateForTests, __resetForTests as __resetPopoutForTests } from '../utils/chatPopout'

const slot = (key: string, title?: string, mode = ''): ChatSlot => ({
  key, title: title ?? key, messages: 0, running: false, mode, created: '', last_ts: '',
})

/** Captures the current URL from MemoryRouter for the new-chat assertion. */
let currentUrl = ''
function UrlCapture() {
  const loc = useLocation()
  const [sp] = useSearchParams()
  currentUrl = loc.pathname + (sp.toString() ? '?' + sp.toString() : '')
  return null
}

function renderChatPage(opts: {
  route?: string
  activeSlot?: string | null
  slots?: ChatSlot[]
  slotsLoaded?: boolean
  slotHistory?: string[]
}) {
  const { route = '/chat', activeSlot = null, slots = [], slotsLoaded = slots.length > 0, slotHistory = [] } = opts
  const preload: PreloadState = {
    dashboard: {
      status: { platform: 'darwin' }, connected: true, slots, slotsLoaded, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    },
    chat: {
      activeSlot, messages: [], slotCursorKey: null, slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'logs', slotActivity: {}, slotHistory,
      slotMessages: {}, slotLoading: false, slotRun: {},
    },
  }
  const store = createTestStore(preload as Partial<RootState>)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const result = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={[route]}>
            <Routes>
              <Route path="/chat/:slug?" element={<ChatPage />} />
              <Route path="/popout/chat/:slug?" element={<ChatPage embedded embedMode="chat" popout />} />
            </Routes>
            <UrlCapture />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { store, ...result }
}

beforeEach(() => {
  localStorage.clear()
  currentUrl = ''
})

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

const slots = [
  slot('chat-1-100', 'Debug video playback'),
  slot('chat-2-200', 'Fix login bug'),
  slot('chat-3-300'), // no title (title === key)
]

describe("a popout's closed-session notice", () => {
  const four = [...slots, slot('chat-4-400', 'Other window')]
  const without = (key: string) => four.filter(s => s.key !== key)
  const notice = () => screen.queryByTestId('popout-session-closed')

  const plainEmpty = () => screen.queryByText(i18nT('pages.chatPage.what_can_i_do_for_you'))

  /** Mount a popout on chat-2-200 and wait for it to show that chat. */
  async function popoutOnOwnChat() {
    const view = renderChatPage({ route: '/popout/chat?sid=chat-2-200', activeSlot: 'chat-2-200', slots: four })
    await waitFor(() => expect(view.store.getState().chat.activeSlot).toBe('chat-2-200'))
    return view
  }

  /** A popout on chat-2-200, then the server's `removed` patch for it, which is
   *  what the gateway sends this bundle when another window closes the chat. */
  async function closedElsewhere() {
    const view = await popoutOnOwnChat()
    await act(async () => { view.store.dispatch(sseSlotPatch({ slots: [], removed: ['chat-2-200'] })) })
    await act(async () => {})
    return view
  }

  it('says the session was closed in another window and offers both actions', async () => {
    await closedElsewhere()
    expect(notice()).toBeTruthy()
    expect(notice()!.getAttribute('role')).toBe('status')
    expect(screen.getByText(i18nT('pages.chatPage.session_closed_in_another_window'))).toBeTruthy()
    expect(screen.getByRole('button', { name: i18nT('pages.chatPage.close_window') })).toBeTruthy()
    expect(screen.getByRole('button', { name: i18nT('pages.chatPage.start_a_new_chat_here') })).toBeTruthy()
    expect(plainEmpty()).toBeNull()
  })

  it('puts keyboard focus on Close window, which names the notice as its description', async () => {
    await closedElsewhere()
    const close = screen.getByTestId('popout-session-closed-close')
    expect(document.activeElement).toBe(close)
    expect(close.getAttribute('aria-describedby')).toBe(notice()!.id)
  })

  it('Close window closes the popout, and falls back to /chat with no sid when the close is refused', async () => {
    const navigated: string[] = []
    __setNavigateForTests(url => { navigated.push(url) })
    const close = vi.spyOn(window, 'close').mockImplementation(() => {})
    try {
      await closedElsewhere()
      act(() => { screen.getByTestId('popout-session-closed-close').click() })
      expect(close).toHaveBeenCalledTimes(1)
      // jsdom never closes the window, which is the refused-close path.
      expect(navigated).toEqual(['/chat'])
    } finally {
      close.mockRestore()
      __resetPopoutForTests()
    }
  })

  it('is shown when the removed chat was the popout\'s only slot', async () => {
    const only = [slot('chat-2-200', 'Fix login bug')]
    const { store } = renderChatPage({ route: '/popout/chat?sid=chat-2-200', activeSlot: 'chat-2-200', slots: only })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
    await act(async () => { store.dispatch(sseSlotPatch({ slots: [], removed: ['chat-2-200'] })) })
    await waitFor(() => expect(notice()).toBeTruthy())
    expect(store.getState().chat.activeSlot).toBeNull()
    expect(document.activeElement).toBe(screen.getByTestId('popout-session-closed-close'))
    // No chat is auto-created behind the notice.
    expect(api.createChatSlot).not.toHaveBeenCalled()
  })

  it('is not shown for a frame that omits the chat until a later frame lists it again', async () => {
    const { store } = await popoutOnOwnChat()
    const reads = vi.mocked(api.chatSlots).mock.calls.length
    await act(async () => { store.dispatch(sseSlots(without('chat-2-200'))) })
    await act(async () => {})
    expect(store.getState().chat.activeSlot).toBeNull()
    expect(notice()).toBeNull()
    expect(screen.queryByTestId('popout-session-closed-close')).toBeNull()
    expect(document.activeElement).toBe(document.body)
    expect(plainEmpty()).toBeTruthy()
    // An omission sends no list read of its own: a read cannot confirm a removal.
    expect(vi.mocked(api.chatSlots).mock.calls.length).toBe(reads)
    await act(async () => { store.dispatch(sseSlots(four)) })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
    expect(notice()).toBeNull()
  })

  it('is not shown while a restarted gateway is still restoring open tabs', async () => {
    const { store } = await popoutOnOwnChat()
    // Mid-restore, every list the server serves omits the tab it has not reached,
    // whether it arrives as a frame or as a read.
    const midRestore = without('chat-2-200')
    vi.mocked(api.chatSlots).mockResolvedValueOnce(midRestore as never)
    await act(async () => {
      store.dispatch(sseConnected())
      store.dispatch(sseSlots(midRestore))
    })
    await act(async () => { await store.dispatch(fetchSlots()) })
    await act(async () => { store.dispatch(sseSlots(midRestore)) })
    await act(async () => {})
    expect(notice()).toBeNull()
    expect(screen.queryByTestId('popout-session-closed-close')).toBeNull()
    expect(document.activeElement).toBe(document.body)
    expect(plainEmpty()).toBeTruthy()
    // The restore reaches the tab and the next frame lists it.
    await act(async () => { store.dispatch(sseSlots(four)) })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
    expect(notice()).toBeNull()
  })

  it('is not shown for a reconnect snapshot that omits the chat for a while', async () => {
    const { store } = await popoutOnOwnChat()
    await act(async () => {
      store.dispatch(sseConnected())
      store.dispatch(sseSlots(without('chat-2-200')))
    })
    await act(async () => {})
    expect(notice()).toBeNull()
    expect(screen.queryByTestId('popout-session-closed-close')).toBeNull()
    // The next frame lists it again and the popout reopens it.
    await act(async () => { store.dispatch(sseSlots(four)) })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
    expect(notice()).toBeNull()
  })

  it('Start a new chat here creates a chat and binds the popout to it', async () => {
    const { store } = await closedElsewhere()
    vi.mocked(api.createChatSlot).mockResolvedValueOnce({ key: 'chat-5-500', title: 'chat-5-500', messages: 0, running: false } as ChatSlot)
    await act(async () => { screen.getByTestId('popout-session-closed-new').click() })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-5-500'))
    expect(api.createChatSlot).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(currentUrl).toMatch(/sid=chat-5-500/))
    expect(notice()).toBeNull()
  })

  it('clears when a later frame lists the chat again', async () => {
    const { store } = await closedElsewhere()
    expect(notice()).toBeTruthy()
    await act(async () => { store.dispatch(sseSlots(four)) })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
    expect(notice()).toBeNull()
  })

  it('is not shown when the popout closed its own chat', async () => {
    const only = [slot('chat-2-200', 'Fix login bug')]
    const { store } = renderChatPage({ route: '/popout/chat?sid=chat-2-200', activeSlot: 'chat-2-200', slots: only })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
    await act(async () => { await store.dispatch(deleteSlot('chat-2-200')) })
    // The same `removed` patch another window's close produces.
    await act(async () => { store.dispatch(sseSlotPatch({ slots: [], removed: ['chat-2-200'] })) })
    await act(async () => {})
    expect(store.getState().chat.activeSlot).toBeNull()
    expect(notice()).toBeNull()
  })

  it('is not shown in a tab, which moves to another chat instead', async () => {
    const { store } = renderChatPage({ route: '/chat?sid=chat-2-200', activeSlot: 'chat-2-200', slots: four, slotHistory: ['chat-1-100'] })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
    await act(async () => { store.dispatch(sseSlotPatch({ slots: [], removed: ['chat-2-200'] })) })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
    expect(notice()).toBeNull()
  })

  it('a popout reloaded while its chat is gone keeps the plain empty state', async () => {
    renderChatPage({ route: '/popout/chat?sid=chat-2-200', slots: without('chat-2-200') })
    await act(async () => {})
    await act(async () => {})
    expect(notice()).toBeNull()
    expect(screen.getByText(i18nT('pages.chatPage.what_can_i_do_for_you'))).toBeTruthy()
  })
})
