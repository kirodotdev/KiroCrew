/**
 * Reopening a chat must land on the tab that chat was left on.
 *
 * Two stores decide what the side panel shows, and they are not the same thing:
 *  - the tab strip (`usePanelTabs`) owns FOCUS and persists it per chat — this
 *    is what the user actually manipulates by clicking a tab chip;
 *  - `chat.activityTab` is a REQUEST channel written only by `openActivityToTab`
 *    (a slash command, a sub-agent / workflow card, a keyboard shortcut).
 *
 * A chat switch restores the incoming chat's cached `activityTab` (Files when it
 * has none), so bridging that value CHANGE into the strip force-focused Files —
 * or whatever view was last requested in that chat — over the tab the strip had
 * remembered. These tests pin the request counter that separates the two, and
 * the two ways the bridge must still fire.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { __resetPanelTabs, usePanelTabs } from '../hooks/usePanelTabs'
import { switchSlot, openActivityToTab, openActivityPanel } from '../store/chatSlice'

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
vi.mock('../pages/chat/ChatSettings', () => ({ loadChatConfig: () => ({ contentWidth: 'compact' }), CONTENT_WIDTH: { compact: { messages: '800px', input: '816px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } } }))
// The strip's focus is the assertion target, so the panel itself is stubbed —
// including `syncPinned`, which lives in the real component and would add the
// pinned views this test does not need.
vi.mock('../pages/chat/SidePanel', () => ({
  default: () => <div data-testid="side-panel" />,
  SIDE_PANEL_MIN_W: 320,
  SIDE_PANEL_RESERVED_W: 560,
  CHAT_PANE_MIN_W: 320,
  measureSidePanelReservedW: () => 560,
  sidePanelFillWidth: () => undefined,
}))
vi.mock('../hooks/usePanelState', () => ({ usePanelState: () => ({ isOpen: false, openPanel: vi.fn(), closePanel: vi.fn() }), useDiffPanel: () => ({ isOpen: false, filePath: '', original: '', modified: '', openDiff: vi.fn(), closeDiff: vi.fn() }) }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
vi.mock('../api/client', () => ({
  api: Object.fromEntries(
    ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
     'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
     'chatSlotWorkspace', 'models', 'planAction', 'planFromChat', 'renameSlot',
     'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
     'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
     'notifications', 'status', 'generateTitle', 'dashboardConfig'].map(k => [k, vi.fn().mockResolvedValue(
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
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

import ChatPage from '../pages/ChatPage'

function renderChat(store: ReturnType<typeof createTestStore>) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat']}>
            <Routes><Route path="/chat/:slug?" element={<ChatPage />} /></Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

describe('ChatPage — side panel focus survives a chat switch', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 1400 })
    localStorage.clear()
    __resetPanelTabs()
  })

  it('reopens a chat on the tab the user left it on, not the last requested view', () => {
    const store = createTestStore()
    renderChat(store)
    const stripA = renderHook(() => usePanelTabs('slot-a'))
    const stripB = renderHook(() => usePanelTabs('slot-b'))

    // Chat A: a sub-agent card opens the panel straight to Subagents.
    act(() => { store.dispatch(switchSlot.pending('r1', 'slot-a')) })
    act(() => { store.dispatch(openActivityToTab('subagents')) })
    expect(stripA.result.current.activeId).toBe('subagents')

    // Chat B: panel open, and the user is looking at Changes.
    act(() => { store.dispatch(switchSlot.pending('r2', 'slot-b')) })
    act(() => { store.dispatch(openActivityPanel()) })
    act(() => { stripB.result.current.openView('changes') })
    expect(stripB.result.current.activeId).toBe('changes')

    // Away and back. Leaving B caches its activityTab as the restore default
    // (Files) and returning restores it, so a value-keyed bridge fired here and
    // pulled focus off Changes onto Files.
    act(() => { store.dispatch(switchSlot.pending('r3', 'slot-a')) })
    act(() => { store.dispatch(switchSlot.pending('r4', 'slot-b')) })

    expect(store.getState().chat.activityOpen).toBe(true)
    expect(stripB.result.current.activeId).toBe('changes')
  })

  it('still honours a deliberate view request after the switch', () => {
    const store = createTestStore()
    renderChat(store)
    const strip = renderHook(() => usePanelTabs('slot-b'))

    act(() => { store.dispatch(switchSlot.pending('r1', 'slot-b')) })
    act(() => { store.dispatch(openActivityToTab('changes')) })
    expect(strip.result.current.activeId).toBe('changes')

    act(() => { store.dispatch(openActivityToTab('workflows')) })
    expect(strip.result.current.activeId).toBe('workflows')
  })

  it('re-focuses a view requested twice with a strip click in between', () => {
    // The counter, not the tab value, is the trigger — so a card asking for the
    // view it already asked for still pulls focus back after the user has
    // clicked away in the strip.
    const store = createTestStore()
    renderChat(store)
    const strip = renderHook(() => usePanelTabs('slot-b'))

    act(() => { store.dispatch(switchSlot.pending('r1', 'slot-b')) })
    act(() => { store.dispatch(openActivityToTab('subagents')) })
    act(() => { strip.result.current.openView('changes') })
    expect(strip.result.current.activeId).toBe('changes')

    act(() => { store.dispatch(openActivityToTab('subagents')) })
    expect(strip.result.current.activeId).toBe('subagents')
  })
})
