/**
 * Tests for ChatPage embedded mode.
 *
 * Verifies that when `embedded={true}`, ChatPage does NOT sync URLs —
 * preventing navigation in the host app when mounted inside an app container.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route, useLocation, useSearchParams } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatSlot } from '../types'
import type { RootState } from '../store'

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
vi.mock('../pages/chat', () => ({ ChatFooter: () => null, AssistantMessage: () => null, McpInfoButton: () => null }))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../pages/chat/ChatSettings', () => ({ loadChatConfig: () => ({ contentWidth: 'compact' }), CONTENT_WIDTH: { compact: { messages: '900px', input: '916px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } } }))

// --- Stub hooks ---
vi.mock('../hooks/usePanelState', () => ({ usePanelState: () => ({ isOpen: false, openPanel: vi.fn(), closePanel: vi.fn() }), useDiffPanel: () => ({ isOpen: false, filePath: '', original: '', modified: '', openDiff: vi.fn(), closeDiff: vi.fn() }) }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))

// --- Stub API ---
vi.mock('../api/client', () => ({
  api: Object.fromEntries(
    ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
     'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
     'chatSlotWorkspace', 'models', 'planAction', 'planFromChat', 'renameSlot',
     'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
     'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
     'notifications', 'status', 'generateTitle'].map(k => [k, vi.fn().mockResolvedValue(
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

const slot = (key: string, title?: string): ChatSlot => ({
  key, title: title ?? key, messages: 0, running: false, mode: '', created: '', last_ts: '',
})

let currentUrl = ''
function UrlCapture() {
  const loc = useLocation()
  const [sp] = useSearchParams()
  currentUrl = loc.pathname + (sp.toString() ? '?' + sp.toString() : '')
  return null
}

function renderEmbedded(opts: {
  route?: string
  activeSlot?: string | null
  slots?: ChatSlot[]
}) {
  const { route = '/apps/workbench', activeSlot = null, slots = [] } = opts
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: false, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as RootState['dashboard'],
    chat: {
      activeSlot, messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
    } as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const result = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={[route]}>
            <Routes>
              <Route path="/apps/workbench" element={<ChatPage embedded />} />
              <Route path="/chat/:slug?" element={<div>Should not navigate here</div>} />
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

afterEach(() => vi.clearAllMocks())

const slots = [
  slot('chat-1-100', 'Debug session'),
  slot('chat-2-200', 'Review session'),
]

describe('ChatPage embedded mode', () => {
  it('does not change URL when activeSlot changes', async () => {
    const { store } = renderEmbedded({ route: '/apps/workbench', slots, activeSlot: 'chat-1-100' })

    await waitFor(() => {
      expect(currentUrl).toBe('/apps/workbench')
    })

    // Change active slot — URL must NOT change to /chat?sid=chat-2-200
    store.dispatch({ type: 'chat/switchSlot/fulfilled', payload: { key: 'chat-2-200', messages: [], has_more: false, total: 0 } })

    // Give effects time to run
    await new Promise(r => setTimeout(r, 100))

    expect(currentUrl).toBe('/apps/workbench')
  })

  it('does not activate slot from ?sid= in URL', async () => {
    renderEmbedded({ route: '/apps/workbench?sid=chat-1-100', slots })

    await new Promise(r => setTimeout(r, 100))

    // URL should stay as-is, not redirect to /chat
    expect(currentUrl).toBe('/apps/workbench?sid=chat-1-100')
    // Should NOT have navigated to /chat
    expect(currentUrl).not.toContain('/chat/')
  })

  it('does not consume pendingInput searchParams', async () => {
    renderEmbedded({ route: '/apps/workbench?autoSend=1&prefill=hello', slots })

    await new Promise(r => setTimeout(r, 100))

    // URL should stay unchanged — embedded mode doesn't consume these params
    expect(currentUrl).toBe('/apps/workbench?autoSend=1&prefill=hello')
  })
})
