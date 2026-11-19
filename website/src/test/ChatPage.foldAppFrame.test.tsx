/**
 * A touch-first phone browser in its mobile mode takes the phone form flat and
 * the desktop form beside a fold, at the same width. Folding or unfolding with
 * an MCP App open in the side panel must keep the panel where it is mounted
 * (panelInlineHost, lib/touchLayoutForm.ts): moving it to the shell's column
 * would mount a second panel and load the app's frame again, losing what was
 * typed or drawn in it. Renders the real ChatPage; the panel body is a stub
 * that draws an iframe while an `app` tab is open.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { __resetPanelTabs } from '../hooks/usePanelTabs'
import { switchSlot, sseMcpAppRender } from '../store/chatSlice'
import type { ViewportSegment } from '../hooks/useViewportSegments'

const device = vi.hoisted(() => ({ isMobile: true, touch: true, segments: null as null | ViewportSegment[] }))
vi.mock('../hooks/useIsMobile', async (orig) => ({
  ...(await orig<typeof import('../hooks/useIsMobile')>()),
  useIsMobile: () => device.isMobile,
}))
vi.mock('../hooks/useViewportSegments', async (orig) => ({
  ...(await orig<typeof import('../hooks/useViewportSegments')>()),
  useViewportSegments: () => device.segments,
}))
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
  // The app tab's frame stands in for McpAppFrame: one iframe per mounted panel.
  default: ({ tabsCtl }: { tabsCtl: { tabs: { kind: string }[] } }) =>
    tabsCtl.tabs.some(t => t.kind === 'app') ? <iframe data-testid="app-frame" title="app" /> : <div data-testid="side-panel" />,
  SIDE_PANEL_MIN_W: 320, SIDE_PANEL_RESERVED_W: 560, CHAT_PANE_MIN_W: 320,
  sidePanelFillWidth: () => undefined,
}))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, k: string) => {
      if (k === 'dashboardConfig') return vi.fn().mockResolvedValue({ mcp_app_panel: true })
      if (k === 'chatSlotDetail') return vi.fn().mockResolvedValue({ messages: [], has_more: false, total: 0 })
      return vi.fn().mockResolvedValue({})
    },
  }),
}))
// A touch-first phone in mobile mode, 951px wide: the width query never
// matches, the fold query matches while a fold is reported.
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: q.includes('pointer: coarse') || q.includes('hover: none') ? device.touch
      : q.includes('viewport-segments') ? device.segments !== null
      : false,
    media: q, onchange: null, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

import ChatPage from '../pages/ChatPage'

const TAB: ViewportSegment[] = [{ x: 0, width: 475.5 }, { x: 475.5, width: 475.5 }]

/** Renders the page; the returned `redraw` renders the same tree again, as a
 *  form or segments change would. */
function renderChat(store: ReturnType<typeof createTestStore>) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const tree = () => (
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat']}>
            <Routes><Route path="/chat/:slug?" element={<ChatPage />} /></Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  const { rerender } = render(tree())
  return { redraw: () => rerender(tree()) }
}

/** Opens an MCP App in the side panel and returns its frame. */
async function openAppFrame(store: ReturnType<typeof createTestStore>): Promise<Element | null> {
  act(() => { store.dispatch(switchSlot.pending('r1', 'slot-a')) })
  await act(async () => { await Promise.resolve() })
  act(() => {
    store.dispatch(sseMcpAppRender({
      session_key: 'slot-a', tool_call_id: 'call-1', server: 'aws-mcp', tool: 'visualize',
      html: '<!doctype html><html><body>app</body></html>', csp: null, permissions: null, spool_id: 'spool-1',
    }))
  })
  await waitFor(() => expect(document.querySelector('[data-testid="app-frame"]')).not.toBeNull())
  return document.querySelector('[data-testid="app-frame"]')
}

/** Sets the window's reported size, as a pane hidden with display:none (0x0)
 *  or shown again would, and fires the resize the page listens for. */
function setViewport(width: number, height: number) {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: height })
  window.dispatchEvent(new Event('resize'))
}

describe('an MCP App frame across a fold on a touch-first phone', () => {
  let shellColumn: HTMLElement
  let ua: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    localStorage.clear()
    __resetPanelTabs()
    device.isMobile = true
    device.touch = true
    device.segments = null
    ua = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Mozilla/5.0 (Linux; Android 14; Fold) Mobile Safari/537.36')
    // The shell's column is on the page, so a desktop-form panel could portal into it.
    shellColumn = document.createElement('div')
    shellColumn.id = 'activity-bar-slot'
    document.body.appendChild(shellColumn)
  })
  afterEach(() => {
    cleanup()
    shellColumn.remove()
    ua.mockRestore()
    setViewport(1024, 768)
  })

  it('keeps the same iframe node, never drawn hidden, through a fold and an unfold', async () => {
    const store = createTestStore()
    const { redraw } = renderChat(store)
    const frame = await openAppFrame(store)
    // Every frame the page draws while the fold moves: the panel's wrapper is
    // never hidden and the app's frame is never replaced.
    const hidden: string[] = []
    const observer = new MutationObserver(() => {
      const wrap = document.querySelector<HTMLElement>('[data-testid="side-panel-inline"]')
      if (!wrap || wrap.style.display === 'none') hidden.push(wrap ? 'hidden' : 'unmounted')
    })
    observer.observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['style'] })

    // Fold: the segments arrive while the phone form is still committed, then
    // the desktop form commits, then the shell slot lookup settles a render later.
    act(() => { device.segments = TAB; redraw() })
    act(() => { device.isMobile = false; redraw() })
    await act(async () => { await Promise.resolve() })
    expect(document.querySelector('[data-testid="app-frame"]')).toBe(frame)
    expect(shellColumn.querySelector('[data-testid="app-frame"]')).toBeNull()

    // Unfold: the segments clear before the phone form commits.
    act(() => { device.segments = null; redraw() })
    act(() => { device.isMobile = true; redraw() })
    await act(async () => { await Promise.resolve() })
    observer.disconnect()
    expect(document.querySelector('[data-testid="app-frame"]')).toBe(frame)
    expect(frame?.isConnected).toBe(true)
    expect(hidden).toEqual([])
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('keeps the same iframe node when a folded pane is hidden to 0x0 and shown again', async () => {
    const store = createTestStore()
    const { redraw } = renderChat(store)
    const frame = await openAppFrame(store)
    act(() => { device.segments = TAB; redraw() })
    act(() => { device.isMobile = false; redraw() })
    await act(async () => { await Promise.resolve() })
    expect(document.querySelector('[data-testid="app-frame"]')).toBe(frame)

    // A remote dashboard pane hidden with display:none reports a 0x0 window;
    // useIsMobile keeps the committed form there.
    act(() => { setViewport(0, 0); redraw() })
    await act(async () => { await Promise.resolve() })
    expect(document.querySelector('[data-testid="app-frame"]')).toBe(frame)
    expect(shellColumn.querySelector('[data-testid="app-frame"]')).toBeNull()

    act(() => { setViewport(951, 600); redraw() })
    await act(async () => { await Promise.resolve() })
    expect(document.querySelector('[data-testid="app-frame"]')).toBe(frame)
    expect(frame?.isConnected).toBe(true)
  })
})
