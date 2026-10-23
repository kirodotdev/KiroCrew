/**
 * The history entries a new session leaves on the chat page.
 *
 * Renders the REAL ChatPage, as ChatPage.sid.test.tsx does, over a history the
 * test holds, so it can model the router committing a push in a transition while
 * an urgent store update renders first.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { act, render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route, useLocation, useSearchParams, useNavigate, unstable_HistoryRouter as HistoryRouter } from 'react-router-dom'
import { UNSAFE_createMemoryHistory as createMemoryHistory } from 'react-router'
import type { ReactNode } from 'react'
import { createTestStore } from './helpers'
import { switchSlot } from '../store/chatSlice'
import { sseSlots } from '../store/dashboardSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

/** Deep-partial preloaded state for createTestStore — test fixtures intentionally
 *  omit fields the reducer fills from initialState. */
type PreloadState = {
  dashboard?: Partial<RootState['dashboard']>
  chat?: Partial<RootState['chat']>
}

// --- Stub child components (same as ChatPage.persist test) ---
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
     // The pane's unrestored-tabs notice reads this on arrival. Unmocked, the read
     // FAILS and the notice says so -- correctly, but it is a second `role="alert"`
     // in a file that counts them. The generic `{}` leaves `reported` unset, which
     // is "no answer yet": the notice renders nothing.
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
import { __resetErrorJournalForTests } from '../utils/errorReport'

const slot = (key: string, title?: string, mode = ''): ChatSlot => ({
  key, title: title ?? key, messages: 0, running: false, mode, created: '', last_ts: '',
})

type MemoryHistory = ReturnType<typeof createMemoryHistory>

/** MemoryRouter, or a HistoryRouter over a history the test holds. */
function PopRouter({ history, children }: { history?: MemoryHistory; children: ReactNode }) {
  return history
    ? <HistoryRouter history={history}>{children}</HistoryRouter>
    : <MemoryRouter initialEntries={['/chat']}>{children}</MemoryRouter>
}

/** Helper to capture the current URL from MemoryRouter */
let currentUrl = ''
let seenUrls: string[] = []
function UrlCapture() {
  const loc = useLocation()
  const [sp] = useSearchParams()
  currentUrl = loc.pathname + (sp.toString() ? '?' + sp.toString() : '')
  if (seenUrls.at(-1) !== currentUrl) seenUrls.push(currentUrl)
  return null
}

/** Exposes the router's navigate() so tests can drive a real Back/Forward POP. */
let navBack: () => void = () => {}
function NavController() {
  const n = useNavigate()
  navBack = () => n(-1)
  return null
}


beforeEach(() => {
  localStorage.clear()
  __resetErrorJournalForTests()
  currentUrl = ''
  seenUrls = []
})

afterEach(() => {
  // Restore real timers here, not only in the tests that install fakes: a fake
  // clock left armed by a failing assertion makes every later test in the file
  // time out, which reads as a cascade of unrelated breakage.
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('new session history entries', () => {
  function renderForPop(initialSlots: ChatSlot[], history?: MemoryHistory) {
    const preload: PreloadState = {
      dashboard: {
        // connected: true is required — the POP-handler effect bails on
        // `if (!connected) return` (so offline tabs don't dispatch a
        // switchSlot that would clear messages). These tests exercise
        // the POP retrace logic itself, which inherently needs the
        // gateway available.
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
            <PopRouter history={history}>
              <Routes>
                <Route path="/chat/:slug?" element={<ChatPage />} />
              </Routes>
              <UrlCapture />
              <NavController />
            </PopRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
    return { store }
  }

  // Regression: New Session pushed TWO identical ?sid= entries, so the first
  // Back after creating a session did nothing. The router commits location
  // changes in a transition, so an urgent re-render right after our push
  // re-ran the URL-sync effect against the OLD ?sid= and pushed again.
  it('creating a session pushes exactly one history entry', async () => {
    const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta')]
    // A real history, so the test can model the race: once armed, the next
    // PUSH is followed by ONE urgent store update (the live `slots` frame that
    // announces a new session) that React renders BEFORE the router's
    // transition commits the new location. One-shot, as in the app: a create
    // produces one frame, and re-arming on every push would turn a duplicate
    // into an unbounded loop instead of the single extra entry users see.
    const history = createMemoryHistory({ initialEntries: ['/chat'], v5Compat: true })
    let storeRef: ReturnType<typeof createTestStore> | null = null
    let armed = false
    const listen = history.listen.bind(history)
    history.listen = (fn) => listen((update) => {
      fn(update)
      if (armed && update.action === 'PUSH' && storeRef) {
        armed = false
        storeRef.dispatch(sseSlots(storeRef.getState().dashboard.slots.map(s => ({ ...s, last_activity_ts: 1 }))))
      }
    })
    const { store } = renderForPop(navSlots, history)
    storeRef = store

    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
    await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
    await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
    const indexBefore = history.index

    // The same action the createSlot thunk resolves with: it activates the
    // slot and adds its row in one commit.
    armed = true
    await act(async () => {
      store.dispatch({
        type: 'chat/createSlot/fulfilled',
        payload: slot('chat-3-300', 'Gamma'),
        meta: { requestId: 'create-1', originActiveSlot: 'chat-2-200', activate: true },
      })
    })
    await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))
    // The injected frame fired, so the race was actually exercised.
    expect(armed).toBe(false)
    expect(history.index).toBe(indexBefore + 1)

    // One Back must leave the new session. A duplicate entry would land on
    // chat-3-300 again and leave the view where it was.
    await act(async () => { navBack() })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
    await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
  })
})
