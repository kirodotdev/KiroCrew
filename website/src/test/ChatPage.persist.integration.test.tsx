/**
 * TRUE regression test for cross-mode active slot persistence.
 *
 * Renders the REAL ChatPage with module-level mocks for child components.
 * If the useEffect cleanup (refs + unmount persist) is removed from ChatPage.tsx,
 * this test FAILS.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, render, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { __resetPanelTabs, usePanelTabs } from '../hooks/usePanelTabs'
import type { ChatSlot } from '../types'
import type { RootState } from '../store'

// --- Stub child components ---
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
     'notifications', 'status'].map(k => [k, vi.fn().mockResolvedValue(k === 'chatSlotDetail' ? { messages: [], has_more: false } : {})])
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

const slot = (key: string, mode = ''): ChatSlot => ({
  key, title: key, messages: 0, running: false, mode, created: '', last_ts: '',
  pending_approval: false, waiting_for_input: false, last_activity_ts: undefined,
})

function renderChatPage(
  mode: string | undefined,
  activeSlot: string | null,
  slots: ChatSlot[],
  chatOverrides: Partial<RootState['chat']> = {},
) {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: false, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot, messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
      ...chatOverrides,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat']}>
            <ChatPage mode={mode} />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  localStorage.clear()
  __resetPanelTabs()
})

const allSlots = [slot('chat-1'), slot('chat-2'), slot('orch-1', 'orchestrator'), slot('orch-2', 'orchestrator')]

describe('ChatPage unmount slot persistence (real component)', () => {
  it('persists activeSlot to localStorage on unmount', async () => {
    const { unmount } = renderChatPage(undefined, 'chat-2', allSlots)
    localStorage.clear()
    await act(() => { unmount() })
    expect(localStorage.getItem('mc-active-slot-chat')).toBe('chat-2')
  })

  it('does not cross-contaminate mode keys', async () => {
    const { unmount } = renderChatPage(undefined, 'chat-2', allSlots)
    localStorage.clear()
    await act(() => { unmount() })
    expect(localStorage.getItem('mc-active-slot-chat')).toBe('chat-2')
    expect(localStorage.getItem('mc-active-slot-orchestrator')).toBeNull()
  })

  it('each mode persists independently', async () => {
    const { unmount: u1 } = renderChatPage(undefined, 'chat-2', allSlots)
    localStorage.clear()
    await act(() => { u1() })
    expect(localStorage.getItem('mc-active-slot-chat')).toBe('chat-2')
    expect(localStorage.getItem('mc-active-slot-orchestrator')).toBeNull()

    const { unmount: u2 } = renderChatPage('orchestrator', 'orch-1', allSlots)
    localStorage.removeItem('mc-active-slot-orchestrator')
    await act(() => { u2() })
    expect(localStorage.getItem('mc-active-slot-orchestrator')).toBe('orch-1')
    expect(localStorage.getItem('mc-active-slot-chat')).toBe('chat-2')
  })

  it('flushes chat draft to localStorage on beforeunload (crash-recovery path)', async () => {
    // Pre-seed a draft in localStorage as if the user had typed before.
    // On mount, ChatPage hydrates the in-memory drafts from localStorage.
    // beforeunload must NOT wipe this draft (regression guard for the
    // flush-before-hydrate and stale-inputRef bugs flagged in review on rev 1).
    localStorage.setItem('mc-chat-drafts', JSON.stringify({ 'chat-2': 'mid-sentence crash content' }))
    localStorage.setItem('mc-chat-drafts-ts', JSON.stringify({ 'chat-2': Date.now() }))
    renderChatPage(undefined, 'chat-2', allSlots)
    await act(async () => { window.dispatchEvent(new Event('beforeunload')) })
    const persisted = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
    expect(persisted['chat-2']).toBe('mid-sentence crash content')
  })

  it('keeps the Changes tab while uncached slot history is loading', async () => {
    vi.mocked(api.chatSlotDetail).mockImplementation(() => new Promise(() => {}))
    const panel = renderHook(() => usePanelTabs('chat-2'))
    act(() => panel.result.current.openView('changes'))
    expect(panel.result.current.tabs.map(tab => tab.id)).toEqual(['changes'])

    renderChatPage(undefined, 'chat-2', allSlots, {
      messages: [],
      slotLoading: true,
    })
    await act(async () => {})

    expect(panel.result.current.tabs.map(tab => tab.id)).toEqual(['changes'])
    expect(panel.result.current.activeId).toBe('changes')
  })

  it('keeps the Changes tab pinned when a settled slot has no source links (regression)', async () => {
    // Before the fix, ChatPage's source-reconcile effect called
    // tabsCtl.closeTab('changes') whenever sourceLinks was empty — fighting
    // SidePanel's always-pinned model and making the Changes tab vanish
    // mid-session (it only reappeared on reload, when syncPinned re-ran).
    // Changes is a permanent pinned tab; an empty source set must NOT close it.
    const panel = renderHook(() => usePanelTabs('chat-2'))
    act(() => panel.result.current.openView('changes'))
    expect(panel.result.current.tabs.map(tab => tab.id)).toEqual(['changes'])

    // Settled hydration (slotLoading: false) + no messages ⇒ no source links,
    // which is exactly the branch that used to auto-close the tab.
    renderChatPage(undefined, 'chat-2', allSlots, {
      messages: [],
      slotLoading: false,
    })
    await act(async () => {})

    expect(panel.result.current.tabs.map(tab => tab.id)).toEqual(['changes'])
  })
})
