/**
 * A fresh embed window opened on `/embed/chat/<key>` must stay on `<key>`.
 *
 * Renders the REAL ChatPage beside the REAL EmbedTabStrip, as App's embed shell
 * does. The strip adds `?sid=<key>` on mount, but the router commits that inside
 * a transition, so the chat page can see the slot list before it sees `?sid=`.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { act, render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
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
import EmbedTabStrip from '../components/EmbedTabStrip'

const slot = (key: string, title: string): ChatSlot => ({
  key, title, messages: 0, running: false, mode: '', created: '', last_ts: '',
})
// The first row is the one the page opens when nothing names a chat.
const slots = [slot('chat-1-100', 'Pinned seed'), slot('chat-2-200', 'Fix login bug')]

let currentUrl = ''
let seenUrls: string[] = []
function UrlCapture() {
  const loc = useLocation()
  currentUrl = loc.pathname + loc.search
  if (seenUrls.at(-1) !== currentUrl) seenUrls.push(currentUrl)
  return null
}

function renderEmbed(route: string, opts: { slotsAtMount: boolean }) {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: true,
      slots: opts.slotsAtMount ? slots : [], slotsLoaded: opts.slotsAtMount,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
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
            <EmbedTabStrip />
            <Routes>
              <Route path="/embed/chat/:slug?" element={<ChatPage embedded embedMode="chat" />} />
              <Route path="/embed/sessions" element={<ChatPage embedded embedMode="sessions" />} />
            </Routes>
            <UrlCapture />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return store
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  currentUrl = ''
  seenUrls = []
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('embed chat deep link in a fresh window', () => {
  it.each([
    ['the slot list is already loaded', true],
    ['the slot list arrives after mount', false],
  ])('stays on the linked chat when %s', async (_label, slotsAtMount) => {
    const store = renderEmbed('/embed/chat/chat-2-200', { slotsAtMount })
    if (!slotsAtMount) await act(async () => { store.dispatch(sseSlots(slots)) })
    await waitFor(() => expect(currentUrl).toBe('/embed/chat/fix-login-bug?sid=chat-2-200'))
    expect(store.getState().chat.activeSlot).toBe('chat-2-200')
    // No step on the way named the default chat or dropped the link's `?sid=`.
    expect(seenUrls.filter(u => u.includes('chat-1-100') || u === '/embed/chat')).toEqual([])
    expect(JSON.parse(sessionStorage.getItem('kirocrew-embed-tabs') ?? '[]')).toEqual(['chat-2-200'])
  })
})
