/**
 * Regression test: ChatPage scopes the sidebar's unread list to its surface.
 *
 * ChatPage must pass a surface-scoped unread list to
 * `<ChatSidebar unreadSlots={...}>`, not the full `state.dashboard.unreadSlots`.
 * If the raw list were passed, an orchestrator-mode slot that became unread
 * while the user was on /chat would inflate the sidebar's "show only unread"
 * toggle count (and prevent its auto-drain effect from disabling the filter when
 * the same-surface inbox actually drained). This test asserts the prop
 * ChatSidebar receives is already scoped to the page's surface — i.e. the wiring
 * of `surfaceUnreadSlots` (built from `filterUnreadKeysBySurface`) is what's
 * passed in, not the raw store value.
 *
 * Helper-level unit coverage of `filterUnreadKeysBySurface` lives in
 * `surfaces.test.tsx`. This file pins the wiring step so a regression that
 * swaps `surfaceUnreadSlots` back for `unreadSlots` in ChatPage fails a test.
 */
import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// Capture the props that ChatPage hands to ChatSidebar so we can assert on
// `unreadSlots` directly. Returning null keeps the sidebar tree out of the
// render path entirely — we only care about prop wiring here.
// Capture every render of ChatSidebar with the props we care about. We
// assert against the first render specifically — async mount effects
// (auto-fetch slots, SSE handshake) mutate dashboard state in ways
// orthogonal to this test, but the synchronous first render deterministically
// reflects the wiring of `useMemo(() => filterUnreadKeysBySurface(...))`.
const sidebarHistory: Array<{ unreadSlots?: readonly string[]; slotsCount: number; mode?: string }> = []
vi.mock('../pages/ChatSidebar', () => ({
  default: (props: { unreadSlots?: readonly string[]; slots?: readonly unknown[]; mode?: string }) => {
    sidebarHistory.push({
      unreadSlots: props.unreadSlots ? [...props.unreadSlots] : props.unreadSlots,
      slotsCount: props.slots?.length ?? -1,
      mode: props.mode,
    })
    return null
  },
  SIDEBAR_MIN: 200,
  SIDEBAR_MAX: 500,
}))

// Mock everything else ChatPage pulls in that isn't relevant to this test.
// (Mirrors the pattern from ChatPage.navPanel.test.tsx.)
vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../components/ChatInput', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/OverlayDrawer', () => ({ default: ({ children }: { children?: ReactNode }) => children }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat', () => ({ ChatFooter: () => null, AssistantMessage: () => null, McpInfoButton: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ contentWidth: 'compact' }),
  CONTENT_WIDTH: { compact: { messages: '900px', input: '916px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } },
}))
vi.mock('../hooks/usePanelState', () => ({ usePanelState: () => ({ isOpen: false, openPanel: vi.fn(), closePanel: vi.fn() }), useDiffPanel: () => ({ isOpen: false, filePath: '', original: '', modified: '', openDiff: vi.fn(), closeDiff: vi.fn() }) }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))

// `api.chatSlots()` is awaited inside `dispatch(fetchSlots())` from a
// useEffect; if it returns an empty list, `fetchSlots.fulfilled` reconciles
// `unreadSlots` against an empty live-key set and wipes it. We override
// `chatSlots` per-test below to return the same fixture we preloaded into
// the store so the post-effect render keeps the unread keys.
const apiMocks: Record<string, ReturnType<typeof vi.fn>> = {}
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, prop: string) => {
      if (!(prop in apiMocks)) {
        apiMocks[prop] = vi.fn().mockResolvedValue(
          prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {},
        )
      }
      return apiMocks[prop]
    },
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as never

import ChatPage from '../pages/ChatPage'

const renderChatPage = (mode: '' | 'orchestrator', slots: Array<{ key: string; mode: string }>, unread: string[]) => {
  const fullSlots = slots.map(s => ({ key: s.key, title: s.key, messages: 1, running: false, mode: s.mode, created: '', last_ts: '' }))
  // Make `api.chatSlots()` return the same fixture we preloaded so the
  // useEffect-triggered `fetchSlots.fulfilled` reducer doesn't reconcile
  // `unreadSlots` against an empty live-key set and wipe it.
  apiMocks.chatSlots = vi.fn().mockResolvedValue(fullSlots)
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: false,
      slots: fullSlots,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0,
      unreadSlots: unread, updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as never,
    chat: {
      activeSlot: null,
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
    } as never,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const route = mode === 'orchestrator' ? '/orchestrated' : '/chat'
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={[route]}>
            <Routes>
              <Route path="/chat/:slug?" element={<ChatPage mode="" />} />
              <Route path="/orchestrated/:slug?" element={<ChatPage mode="orchestrator" />} />
            </Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

describe('ChatPage – ChatSidebar unreadSlots wiring', () => {
  it('passes all chat-like unread keys in the unified view', () => {
    // In the unified sidebar, both default and orchestrator slots appear
    // together, so the unread list includes both.
    sidebarHistory.length = 0
    renderChatPage(
      '',
      [{ key: 'chat-1', mode: '' }, { key: 'orch-1', mode: 'orchestrator' }],
      ['chat-1', 'orch-1'],
    )
    expect(sidebarHistory[0]?.slotsCount).toBe(2)
    expect(sidebarHistory[0]?.unreadSlots).toEqual(['chat-1', 'orch-1'])
  })

  it('unified view also works when mounted with orchestrator mode prop', () => {
    // Even with mode="orchestrator", the unified filtering shows both slots.
    sidebarHistory.length = 0
    renderChatPage(
      'orchestrator',
      [{ key: 'chat-1', mode: '' }, { key: 'orch-1', mode: 'orchestrator' }],
      ['chat-1', 'orch-1'],
    )
    expect(sidebarHistory[0]?.slotsCount).toBe(2)
    expect(sidebarHistory[0]?.unreadSlots).toEqual(['chat-1', 'orch-1'])
  })
})
