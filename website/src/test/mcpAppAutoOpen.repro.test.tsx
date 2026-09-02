/**
 * REPRO harness for #2118 — does an MCP App's side panel auto-open on the
 * FIRST render when dashboard.mcp_app_panel is on?
 *
 * Renders the REAL ChatPage and drives the real sequence: config query resolves
 * mcp_app_panel:true, an mcp_app_render lands in the store for the active slot,
 * and we assert the panel opened AND an `app` tab exists for that slot. Both
 * orderings are exercised (config-before-render and render-before-config), since
 * the config query gate is a named suspect.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, renderHook, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { __resetPanelTabs, usePanelTabs } from '../hooks/usePanelTabs'
import { switchSlot, sseMcpAppRender } from '../store/chatSlice'

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
vi.mock('../pages/chat/SidePanel', () => ({
  default: () => <div data-testid="side-panel" />,
  SIDE_PANEL_MIN_W: 320, SIDE_PANEL_RESERVED_W: 560, CHAT_PANE_MIN_W: 320,
  measureSidePanelReservedW: () => 560, sidePanelFillWidth: () => undefined,
}))
vi.mock('../hooks/usePanelState', () => ({ usePanelState: () => ({ isOpen: false, openPanel: vi.fn(), closePanel: vi.fn() }), useDiffPanel: () => ({ isOpen: false, filePath: '', original: '', modified: '', openDiff: vi.fn(), closeDiff: vi.fn() }) }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

const dashboardConfig = vi.fn().mockResolvedValue({ mcp_app_panel: true })
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, k: string) => {
      if (k === 'dashboardConfig') return dashboardConfig
      if (k === 'chatSlotDetail') return vi.fn().mockResolvedValue({ messages: [], has_more: false, total: 0 })
      return vi.fn().mockResolvedValue({})
    },
  }),
}))
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({ matches: false, media: q, onchange: null, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() })),
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

const renderPayload = (session_key: string, tool_call_id: string) => ({
  session_key, tool_call_id, server: 'aws-mcp', tool: 'visualize',
  html: '<!doctype html><html><body>app</body></html>', csp: null, permissions: null,
  spool_id: 'spool-1',
})

describe('#2118 — MCP app side panel auto-opens on first render', () => {
  beforeEach(() => { localStorage.clear(); __resetPanelTabs(); dashboardConfig.mockClear() })

  it('opens the panel + an app tab when config resolves BEFORE the render arrives', async () => {
    const store = createTestStore()
    renderChat(store)
    const strip = renderHook(() => usePanelTabs('slot-a'))
    act(() => { store.dispatch(switchSlot.pending('r1', 'slot-a')) })
    // let the dashboardConfig query resolve mcp_app_panel:true
    await waitFor(() => expect(dashboardConfig).toHaveBeenCalled())
    await act(async () => { await Promise.resolve() })

    act(() => { store.dispatch(sseMcpAppRender(renderPayload('slot-a', 'call-1'))) })

    await waitFor(() => {
      expect(strip.result.current.tabs.some(t => t.id === 'app:call-1')).toBe(true)
    })
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('opens the panel + an app tab when the render arrives BEFORE config resolves', async () => {
    const store = createTestStore()
    renderChat(store)
    const strip = renderHook(() => usePanelTabs('slot-a'))
    act(() => { store.dispatch(switchSlot.pending('r1', 'slot-a')) })
    // render lands first, while mcp_app_panel is still unknown
    act(() => { store.dispatch(sseMcpAppRender(renderPayload('slot-a', 'call-1'))) })

    // then config resolves true — the effect must re-fire and open
    await waitFor(() => {
      expect(strip.result.current.tabs.some(t => t.id === 'app:call-1')).toBe(true)
    })
    expect(store.getState().chat.activityOpen).toBe(true)
  })
})
