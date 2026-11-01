/**
 * Back/Forward (history POP) onto a session closed since it was visited.
 *
 * Split out of ChatPage.sid.test.tsx (file line cap). Renders the REAL ChatPage
 * with the same child-component and API stubs as that suite, and drives a real
 * MemoryRouter Back/Forward.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { act, render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route, useLocation, useSearchParams, useNavigate } from 'react-router-dom'
import { createTestStore } from './helpers'
import { deleteSlot, switchSlot } from '../store/chatSlice'
import { sseSlotPatch } from '../store/dashboardSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

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
import { api } from '../api/client'
import { __resetErrorJournalForTests } from '../utils/errorReport'

const slot = (key: string, title?: string, mode = ''): ChatSlot => ({
  key, title: title ?? key, messages: 0, running: false, mode, created: '', last_ts: '',
})

let currentUrl = ''
function UrlCapture() {
  const loc = useLocation()
  const [sp] = useSearchParams()
  currentUrl = loc.pathname + (sp.toString() ? '?' + sp.toString() : '')
  return null
}

let navBack: () => void = () => {}
let navForward: () => void = () => {}
function NavController() {
  const n = useNavigate()
  navBack = () => n(-1)
  navForward = () => n(1)
  return null
}

beforeEach(() => {
  localStorage.clear()
  __resetErrorJournalForTests()
  currentUrl = ''
})

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('ChatPage browser Back/Forward onto a closed session', () => {
  function renderForPop(initialSlots: ChatSlot[]) {
    const preload: PreloadState = {
      dashboard: {
        // connected: the POP-handler effect bails while offline.
        status: { platform: 'darwin' }, connected: true, slots: initialSlots, slotsLoaded: true, approvalMode: 'normal',
        channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
        sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      },
      chat: {
        activeSlot: null, messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
        slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
        pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'logs', slotActivity: {}, slotHistory: [],
        slotMessages: {}, slotLoading: false,
      },
    }
    const store = createTestStore(preload as Partial<RootState>)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter initialEntries={['/chat']}>
              <Routes>
                <Route path="/chat/:slug?" element={<ChatPage />} />
              </Routes>
              <UrlCapture />
              <NavController />
            </MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
    return { store }
  }

  /** Visit A -> B -> C, then close B and let the server confirm it with a
   *  durable `removed` frame (what sets `durablyRemoved`). */
  async function visitThenCloseMiddle(store: ReturnType<typeof createTestStore>) {
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
    await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
    await act(async () => { await store.dispatch(switchSlot('chat-3-300')) })
    await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))

    let finishClose!: () => void
    vi.mocked(api.deleteChatSlot).mockReturnValueOnce(new Promise<void>(resolve => { finishClose = resolve }) as never)
    vi.mocked(api.chatSlotDetail).mockImplementation((key: string) =>
      key === 'chat-2-200'
        ? Promise.reject(Object.assign(new Error('not found'), { status: 404 }))
        : Promise.resolve({ messages: [], has_more: false, total: 0 }),
    )
    let closing!: Promise<unknown>
    act(() => { closing = store.dispatch(deleteSlot('chat-2-200')) })
    await waitFor(() => expect(store.getState().dashboard.slots.map(s => s.key)).not.toContain('chat-2-200'))
    await act(async () => { store.dispatch(sseSlotPatch({ slots: [], removed: ['chat-2-200'] })) })
    await act(async () => { finishClose(); await closing })
    expect(store.getState().dashboard.durablyRemoved).toHaveProperty('chat-2-200')
  }

  const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta'), slot('chat-3-300', 'Gamma')]

  afterEach(() => {
    vi.mocked(api.chatSlotDetail).mockReset()
    vi.mocked(api.chatSlotDetail).mockResolvedValue({ messages: [], has_more: false, total: 0 } as never)
  })

  // Regression (closed-session dead-end): a Back POP whose target was CLOSED
  // since the entry was pushed used to repair the URL silently, so the view
  // snapped back with no explanation. The server-confirmed-gone frame now
  // surfaces the notice via `announceSlotGone` (NOT a `switchSlot` fetch, which
  // would move activeSlot onto the unlisted key and tear the user off it).
  it('announces and self-heals when Back targets a session closed since it was visited', async () => {
    const { store } = renderForPop(navSlots)
    await visitThenCloseMiddle(store)

    await act(async () => { navBack() })
    await waitFor(() => expect(store.getState().chat.switchSlotGone).not.toBeNull())
    expect(store.getState().chat.switchSlotGone?.kind).toBe('gone')
    // Unnamed gone (the row already left dashboard.slots) -> gesture-neutral copy.
    expect(store.getState().chat.switchSlotGone?.gestureNeutral).toBe(true)
    // The user stays on the live session, never torn off onto null.
    expect(store.getState().chat.activeSlot).toBe('chat-3-300')
    await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))
    expect(currentUrl).not.toContain('sid=chat-2-200')
    // The repair is a REPLACE, not a PUSH, so the forward entry is intact.
    await act(async () => { navForward() })
    await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))
  })

  // Regression (stale gone-notice): a later Back onto a LIVE session dispatches
  // a plain `switchSlot(urlSid)`, which `switchSlot.pending` does not treat as
  // an announced gesture. The POP branch clears the notice itself first.
  it('clears the gone notice when a later Back lands on a live session', async () => {
    const { store } = renderForPop(navSlots)
    await visitThenCloseMiddle(store)

    await act(async () => { navBack() })
    await waitFor(() => expect(store.getState().chat.switchSlotGone).not.toBeNull())
    expect(store.getState().chat.activeSlot).toBe('chat-3-300')

    await act(async () => { navBack() })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
    expect(store.getState().chat.switchSlotGone).toBeNull()
  })
})
