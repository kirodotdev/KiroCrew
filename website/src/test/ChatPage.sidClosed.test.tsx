/**
 * #29460: a full-key `?sid=` link (or a tab reloading one) whose session is no
 * longer open but still has a transcript on disk. The 5 s deadline used to
 * answer `Session "<key>" not found` with no way back; it now probes that one
 * key and, when a transcript exists, says the session was closed and offers
 * Resume through the history resume path. A key with no transcript, or a
 * probe that fails, keeps "not found". A slots frame that brings the key back
 * while the probe runs still opens the session, with no notice.
 *
 * Same child stubs as ChatPage.sid; kept apart from it so that file stays under
 * the line cap.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
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
     'chatSlotsUnrestored', 'sessionsNamed', 'sessionMeta'].map(k => [k, vi.fn().mockResolvedValue(
      k === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : k === 'sessionMeta' ? null : {}
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
import { i18nT } from '../i18n/t'

const OPEN = 'chat-1-100'
const CLOSED = 'chat-9-900'
const slot = (key: string): ChatSlot => ({ key, title: key, messages: 0, running: false, mode: '', created: '', last_ts: '' })

function renderLink(route: string) {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: true, slots: [slot(OPEN)], slotsLoaded: true, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    },
    chat: {
      activeSlot: null, messages: [], slotCursorKey: null, slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'logs', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false, slotRun: {},
    },
  } as unknown as Partial<RootState>)
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
  return store
}

/** Runs the fake clock inside act(), so every update it releases is flushed before the next assertion. */
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })

/** Opens the link on a fake clock and runs it past the 5 s deadline. */
async function openPastDeadline(route: string) {
  vi.useFakeTimers()
  const store = renderLink(route)
  await advance(5100)
  return store
}

const resumeButton = () => screen.queryByRole('button', { name: i18nT('components.notifications.notificationDetailPanel.resume_chat') })

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('a full-key ?sid= link to a session that is not open', () => {
  it('offers Resume when the session has a transcript, and Resume reopens it', async () => {
    vi.mocked(api.sessionMeta).mockResolvedValueOnce({ key: `dashboard_${CLOSED}`, title: 'Old work' })
    vi.mocked(api.resumeChatSlot).mockResolvedValueOnce({ ok: true, key: CLOSED, mode: '', messages: [] } as never)
    const store = await openPastDeadline(`/chat?sid=${CLOSED}`)

    const notice = screen.getByTestId('sid-error')
    expect(notice).toHaveTextContent(i18nT('store.chatSlice.session_closed_find_in_older_sessions'))
    expect(screen.queryByText(new RegExp(`session "${CLOSED}" not found`, 'i'))).toBeNull()
    expect(api.sessionMeta).toHaveBeenCalledWith(CLOSED)

    await act(async () => { fireEvent.click(resumeButton()!) })
    await advance(50)
    expect(api.resumeChatSlot).toHaveBeenCalledWith(`dashboard_${CLOSED}`, 'Old work')
    expect(store.getState().chat.activeSlot).toBe(CLOSED)
    expect(screen.queryByTestId('sid-error')).toBeNull()
  })

  it('keeps "not found" and offers nothing when the key has no transcript', async () => {
    await openPastDeadline(`/chat?sid=${CLOSED}`)

    expect(screen.getByText(new RegExp(`session "${CLOSED}" not found`, 'i'))).toBeTruthy()
    expect(resumeButton()).toBeNull()
  })

  it('keeps "not found" when the probe fails', async () => {
    vi.mocked(api.sessionMeta).mockRejectedValueOnce(new Error('offline'))
    await openPastDeadline(`/chat?sid=${CLOSED}`)

    expect(screen.getByText(new RegExp(`session "${CLOSED}" not found`, 'i'))).toBeTruthy()
    expect(resumeButton()).toBeNull()
  })

  it('opens the session, with no notice, when a slots frame brings the key back while the probe runs', async () => {
    let answer: (row: { key: string; title: string }) => void = () => {}
    vi.mocked(api.sessionMeta).mockReturnValueOnce(new Promise(r => { answer = r }))
    const store = await openPastDeadline(`/chat?sid=${CLOSED}`)
    expect(api.sessionMeta).toHaveBeenCalledWith(CLOSED)

    await act(async () => { store.dispatch(sseSlots([slot(OPEN), slot(CLOSED)])) })
    await act(async () => { answer({ key: `dashboard_${CLOSED}`, title: 'Old work' }) })
    await advance(50)

    expect(store.getState().chat.activeSlot).toBe(CLOSED)
    expect(screen.queryByTestId('sid-error')).toBeNull()
  })
})
