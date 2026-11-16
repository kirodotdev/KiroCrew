/**
 * The URL's session opens one turn of the main thread after the session list
 * names it, not in the task the list arrived in.
 *
 * That task has just rendered the sidebar; opening the session there mounts
 * the transcript in the same task, the longest one of a cold open. These tests
 * pin the wait and what can happen during it: nothing changes, a session the
 * user picks wins, and an unmounted page opens nothing.
 *
 * A file of its own because `ChatPage.sid.test.tsx` is at the line cap.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import { createTestStore } from './helpers'
import { switchSlot } from '../store/chatSlice'
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
     'chatSlotsUnrestored', 'sessionsNamed'].map(k => [k, vi.fn().mockResolvedValue(
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
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

import ChatPage from '../pages/ChatPage'

const slot = (key: string, title: string): ChatSlot => ({
  key, title, messages: 0, running: false, mode: '', created: '', last_ts: '',
})
const FIRST = slot('chat-1-100', 'Alpha')
const LINKED = slot('chat-2-200', 'Beta')

/** Every URL the router rendered, in order. */
let seenUrls: string[] = []
function UrlCapture() {
  const { pathname, search } = useLocation()
  const url = pathname + search
  if (seenUrls.at(-1) !== url) seenUrls.push(url)
  return null
}

/** The chat page on `/chat?sid=<LINKED>` with the session list already known. */
function openLink() {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: true, slots: [FIRST, LINKED], slotsLoaded: true, approvalMode: 'normal',
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
  } as unknown as Partial<RootState>)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={[`/chat?sid=${LINKED.key}`]}>
            <Routes>
              <Route path="/chat/:slug?" element={<ChatPage />} />
            </Routes>
            <UrlCapture />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { store, ...view }
}

/** One turn of the main thread: the open's zero-delay timer, and the updates it schedules. */
const takeTurn = () => act(async () => { await vi.advanceTimersByTimeAsync(0) })

beforeEach(() => {
  localStorage.clear()
  seenUrls = []
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('the URL session opens one turn after the list is known', () => {
  it('is not open when mount returns, leaves the URL alone meanwhile, and opens on the next turn', async () => {
    const { store } = openLink()
    // Mount ran with the list present: nothing opened, nothing rewritten.
    expect(store.getState().chat.activeSlot).toBeNull()
    expect(seenUrls).toEqual([`/chat?sid=${LINKED.key}`])

    await takeTurn()
    expect(store.getState().chat.activeSlot).toBe(LINKED.key)
  })

  it('a session picked during that turn wins over the URL session', async () => {
    const { store } = openLink()
    const shown: Array<string | null> = []
    store.subscribe(() => {
      const key = store.getState().chat.activeSlot
      if (shown.at(-1) !== key) shown.push(key)
    })
    await act(async () => { await store.dispatch(switchSlot(FIRST.key)) })
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    // The link was abandoned by the pick: its session never came on screen.
    expect(shown).toEqual([FIRST.key])
    expect(seenUrls.at(-1)).toContain(`sid=${FIRST.key}`)
  })

  it('a page that unmounts during that turn opens nothing', async () => {
    const { store, unmount } = openLink()
    unmount()
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    expect(store.getState().chat.activeSlot).toBeNull()
  })
})
