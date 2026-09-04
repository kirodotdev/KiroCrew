/**
 * Integration test for ChatPage search wiring.
 * Follows the pattern from ChatPage.persist.integration.test.tsx —
 * stubs heavy child components, uses real ChatPage with real hooks.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../src/hooks/useTheme'
import type { ChatSlot, ChatMessage } from '../src/types'

// --- Stub child components ---
vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../src/components/ChatInput', () => ({ default: () => null }))
vi.mock('../src/components/WelcomeView', () => ({ default: () => null }))
vi.mock('../src/components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../src/components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../src/components/TypewriterText', () => ({ default: () => null }))
vi.mock('../src/components/OverlayDrawer', () => ({ default: () => null }))
vi.mock('../src/components/AgentDropdownList', () => ({ default: () => null }))
vi.mock('../src/components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../src/components/InfoTip', () => ({ default: () => null }))
vi.mock('../src/components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../src/pages/chat/CollapsibleToolGroup', () => ({ default: () => null }))
vi.mock('../src/pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../src/pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../src/pages/chat', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/pages/chat')>()
  return {
    ...actual,
    ChatFooter: () => null,
    AssistantMessage: () => null,
    UserMessage: () => null,
    McpInfoButton: () => null,
  }
})
vi.mock('../src/pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../src/pages/chat/ChatSettings', () => ({ loadChatConfig: () => ({ contentWidth: 'compact' }), CONTENT_WIDTH: { compact: { messages: '900px', input: '916px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } } }))

// --- Stub hooks ---
vi.mock('../src/hooks/usePanelState', () => ({ usePanelState: () => ({ isOpen: false, openPanel: vi.fn(), closePanel: vi.fn() }), useDiffPanel: () => ({ isOpen: false, filePath: '', original: '', modified: '', openDiff: vi.fn(), closeDiff: vi.fn() }) }))
vi.mock('../src/hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../src/hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../src/hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../src/hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))

// --- Stub API ---
vi.mock('../src/api/client', () => ({
  api: Object.fromEntries(
    ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
     'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
     'chatSlotWorkspace', 'models', 'planAction', 'planFromChat', 'renameSlot',
     'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
     'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
     'notifications', 'status', 'mcpActive'].map(k => [k, vi.fn().mockResolvedValue(
       k === 'chatSlotDetail' ? { messages: [
         { role: 'user', content: 'hello world', ts: '2026-01-01T00:00:00Z' },
         { role: 'assistant', content: 'hi there world', ts: '2026-01-01T00:01:00Z' },
       ], has_more: false } : {}
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
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as any

import ChatPage from '../src/pages/ChatPage'

const msg = (role: string, content: string): ChatMessage => ({ role, content, cls: '', ts: new Date().toISOString() })

const testMessages: ChatMessage[] = [
  msg('user', 'hello world'),
  msg('assistant', 'hi there world'),
  msg('tool', 'tool output with world'),
  msg('user', 'another message'),
  msg('assistant', 'world again'),
]

const slot: ChatSlot = {
  key: 'slot-1', title: 'Test', messages: 5, running: false, created: '', last_ts: '',
}

function renderChatPage(messages: ChatMessage[]) {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: true, slots: [slot], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {}, slotsLoaded: true,
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as any,
    chat: {
      activeSlot: 'slot-1', messages, slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    } as any,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return {
    store,
    ...render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter initialEntries={['/chat?sid=slot-1']}>
              <ChatPage />
            </MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    ),
  }
}

beforeEach(() => localStorage.clear())

describe('ChatPage in-session search integration', () => {
  it('Cmd+F opens search state (SearchBar renders in chat area)', async () => {
    renderChatPage(testMessages)
    expect(screen.queryByPlaceholderText('Find in chat…')).not.toBeInTheDocument()
    await act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', metaKey: true, bubbles: true })) })
    expect(screen.getByPlaceholderText('Find in chat…')).toBeInTheDocument()
  })

  it('Escape closes SearchBar', async () => {
    renderChatPage(testMessages)
    await act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', metaKey: true, bubbles: true })) })
    expect(screen.getByPlaceholderText('Find in chat…')).toBeInTheDocument()
    await act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })) })
    expect(screen.queryByPlaceholderText('Find in chat…')).not.toBeInTheDocument()
  })
})
