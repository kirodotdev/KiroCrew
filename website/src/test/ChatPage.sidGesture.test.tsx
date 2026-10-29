/**
 * #23564: a cron note is read when the user opens its chat by a gesture
 * (`ChatState.userOpen`). Through the REAL ChatPage, which switches on its own
 * in several places, this pins which of those switches count:
 *
 * - The command palette can open a chat from another page: it dispatches the
 *   gesture switch, then navigates to /chat. ChatPage's mount re-fetch must not
 *   replace that in-flight switch with a plain one, which would drop the
 *   gesture and leave the note unread.
 * - A `?sid=` link the page resolves late, once its slot appears, is the page's
 *   own switch. It records no user open, so it reads no note and cannot undo a
 *   "Mark unread".
 *
 * Same child stubs as ChatPage.sid; kept apart from it so that file stays under
 * the line cap.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { switchSlot } from '../store/chatSlice'
import { sseSlots } from '../store/dashboardSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

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
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../api/client', () => ({
  api: Object.fromEntries(
    ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
     'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
     'chatSlotWorkspace', 'models', 'planFromChat', 'renameSlot',
     'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
     'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
     'notifications', 'status', 'generateTitle', 'kirocrewConfig', 'agentResolvedModel',
     'chatSlotsUnrestored', 'sessionsNamed', 'setSlotColor'].map(k => [k, vi.fn().mockResolvedValue(
      k === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {}
    )])
  ),
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
import { api } from '../api/client'

const CRON = 'cron-a'
const OTHER = 'chat-1-100'
const slot = (key: string): ChatSlot => ({ key, title: key, messages: 0, running: false, mode: '', created: '', last_ts: '' })

/** The same preload as ChatPage.sid, on *activeSlot*. */
function makeStore(activeSlot: string | null, slots: ChatSlot[], slotsLoaded = slots.length > 0) {
  return createTestStore({
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
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'logs', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false, slotRun: {},
    },
  } as unknown as Partial<RootState>)
}

function renderChatPage(store: ReturnType<typeof createTestStore>, route = '/chat') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={[route]}>
            <Routes><Route path="/chat/:slug?" element={<ChatPage />} /></Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('ChatPage switches and the user open (#23564)', () => {
  it('a mount while a gesture open is in flight keeps the open, so its landing records the user open', async () => {
    let land!: (v: unknown) => void
    vi.mocked(api.chatSlotDetail).mockImplementationOnce(() => new Promise(res => { land = res }) as never)
    const store = makeStore(OTHER, [slot(OTHER), slot(CRON)])
    // The palette on another page: the gesture switch starts, its transcript still loading.
    void store.dispatch(switchSlot({ key: CRON, announceOnMissing: true, gesture: true }))
    expect(store.getState().chat.activeSlot).toBe(CRON)
    renderChatPage(store)
    // ChatPage mounted on the same chat: no second, plain fetch replaced the open.
    expect(vi.mocked(api.chatSlotDetail)).toHaveBeenCalledTimes(1)
    await act(async () => { land({ messages: [], has_more: false, total: 0 }); await Promise.resolve() })
    expect(store.getState().chat.userOpen?.key).toBe(CRON)
  })

  it('a ?sid= link resolved late by the page lands its chat and records no user open', async () => {
    vi.useFakeTimers()
    const LATE = 'chat-9-900'
    const listed = [slot(OTHER), slot('chat-2-200')]
    const store = makeStore(OTHER, [], false)
    renderChatPage(store, `/chat?sid=${LATE}`)
    await act(async () => { store.dispatch(sseSlots(listed)) })
    // The 5s deadline declares the link missing; a frame inside the recovery window then carries it.
    const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })
    await advance(5100)
    await advance(10_000)
    await act(async () => { store.dispatch(sseSlots([...listed, slot(LATE)])) })
    await advance(50)
    expect(store.getState().chat.activeSlot).toBe(LATE)
    expect(store.getState().chat.slotSwitchRequestId).toBeNull()
    expect(store.getState().chat.userOpen).toBeUndefined()
  })
})
