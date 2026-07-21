/**
 * Tests for ChatPage's responsive activity panel behaviors:
 * - auto-collapse when the window shrinks below the panel's space threshold
 * - auto-reopen (with hysteresis) when space returns — only if it was the
 *   auto-collapse that closed it
 * - a manual toggle cancels any pending auto-reopen (AutoSDE post 12 fix)
 * - portal slot self-healing: if the actbar slot div isn't in the DOM when
 *   ChatPage looks for it, a MutationObserver latches it when it appears
 *   (fixes the mobile->desktop race that stranded the panel inline).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, renderHook, act, waitFor, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { __resetPanelTabs, usePanelTabs } from '../hooks/usePanelTabs'
import { switchSlot, toggleActivity } from '../store/chatSlice'

// --- Stub child components (same scaffold as ChatPage.embedded test) ---
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
// SidePanel: stub the component (pulls in xterm etc.) but keep the space
// contract deterministic: threshold = 320 + 560 = 880, reopen at 920.
vi.mock('../pages/chat/SidePanel', () => ({
  default: () => <div data-testid="side-panel" />,
  SIDE_PANEL_MIN_W: 320,
  SIDE_PANEL_RESERVED_W: 560,
  measureSidePanelReservedW: () => 560,
}))

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
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as any
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as any

import ChatPage from '../pages/ChatPage'

const setWindowWidth = (w: number) => {
  Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: w })
}
const resizeTo = (w: number) => act(() => {
  setWindowWidth(w)
  window.dispatchEvent(new Event('resize'))
})

function renderChat(store = createTestStore()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const utils = render(
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
  return { store, queryClient, ...utils }
}

describe('ChatPage — pull request panel discovery', () => {
  beforeEach(() => {
    setWindowWidth(1400)
    localStorage.clear()
    __resetPanelTabs()
  })

  it('selects the Changes tab for a detected pull request without opening the panel', async () => {
    const store = createTestStore()
    act(() => {
      store.dispatch(switchSlot.pending('request-pr', 'slot-pr'))
      store.dispatch(switchSlot.fulfilled({
        key: 'slot-pr',
        messages: [{
          role: 'assistant',
          content: 'Review https://github.com/kirodotdev/KiroCrew/pull/119',
          cls: '',
        }],
        running: false,
        hasMore: false,
        total: 1,
        queue: [],
      }, 'request-pr', 'slot-pr'))
    })
    const panelTabs = renderHook(() => usePanelTabs('slot-pr'))

    renderChat(store)

    await waitFor(() => {
      expect(panelTabs.result.current.tabs.map(tab => tab.id)).toEqual(['changes'])
      expect(panelTabs.result.current.activeId).toBe('changes')
    })
    expect(store.getState().chat.activityOpen).toBe(false)
    expect(screen.queryByTestId('side-panel')).not.toBeInTheDocument()

    act(() => { store.dispatch(toggleActivity()) })
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(await screen.findByTestId('side-panel')).toBeInTheDocument()
  })
})

describe('ChatPage — responsive activity panel auto-collapse', () => {
  beforeEach(() => setWindowWidth(1400))
  afterEach(() => {
    document.getElementById('activity-bar-slot')?.remove()
  })

  it('auto-collapses when the window crosses below the space threshold (880)', () => {
    const { store } = renderChat()
    act(() => { store.dispatch(toggleActivity()) })
    expect(store.getState().chat.activityOpen).toBe(true)

    resizeTo(850) // crosses 1400 -> 850, below 880
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('auto-reopens when space returns past the hysteresis point (920), only after an auto-collapse', () => {
    const { store } = renderChat()
    act(() => { store.dispatch(toggleActivity()) })
    resizeTo(850)
    expect(store.getState().chat.activityOpen).toBe(false)

    // Not enough clearance yet (below threshold+40): stays closed.
    resizeTo(900)
    expect(store.getState().chat.activityOpen).toBe(false)

    // Crosses 920: reopens because the collapse was automatic.
    resizeTo(1000)
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('does NOT auto-reopen a panel the user closed manually', () => {
    const { store } = renderChat()
    act(() => { store.dispatch(toggleActivity()) })
    // Manual close via the header/panel toggle event.
    act(() => { window.dispatchEvent(new CustomEvent('toggle-activity-panel')) })
    expect(store.getState().chat.activityOpen).toBe(false)

    resizeTo(850)
    resizeTo(1000)
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('a manual open+close after an auto-collapse cancels the pending auto-reopen', () => {
    const { store } = renderChat()
    act(() => { store.dispatch(toggleActivity()) })
    resizeTo(850) // auto-collapse: pending reopen armed
    expect(store.getState().chat.activityOpen).toBe(false)

    // User manually opens then closes on the narrow window.
    act(() => { window.dispatchEvent(new CustomEvent('toggle-activity-panel')) })
    expect(store.getState().chat.activityOpen).toBe(true)
    act(() => { window.dispatchEvent(new CustomEvent('toggle-activity-panel')) })
    expect(store.getState().chat.activityOpen).toBe(false)

    // Widening must respect the explicit close — no surprise reopen.
    resizeTo(1000)
    expect(store.getState().chat.activityOpen).toBe(false)
  })
})

describe('ChatPage — activity slot self-healing', () => {
  beforeEach(() => setWindowWidth(1400))
  afterEach(() => {
    document.getElementById('activity-bar-slot')?.remove()
  })

  it('renders the panel inline when no slot exists, then migrates into a slot that appears later', async () => {
    const { store, container } = renderChat()
    act(() => { store.dispatch(toggleActivity()) })

    // No slot div in the DOM -> inline fallback inside ChatPage's own tree.
    const inline = await screen.findByTestId('side-panel')
    expect(container.contains(inline)).toBe(true)

    // The App shell (re)creates the slot — e.g. after a mobile -> desktop
    // crossing where ChatPage's lookup ran before the shell re-rendered.
    // The MutationObserver must latch it and portal the panel there.
    const slot = document.createElement('div')
    slot.id = 'activity-bar-slot'
    act(() => { document.body.appendChild(slot) })

    await waitFor(() => {
      const panel = screen.getByTestId('side-panel')
      expect(slot.contains(panel)).toBe(true)
    })
  })

  it('uses the slot directly when it already exists at mount', async () => {
    const slot = document.createElement('div')
    slot.id = 'activity-bar-slot'
    document.body.appendChild(slot)

    const { store } = renderChat()
    act(() => { store.dispatch(toggleActivity()) })

    await waitFor(() => {
      const panel = screen.getByTestId('side-panel')
      expect(slot.contains(panel)).toBe(true)
      expect(panel.parentElement).toHaveClass('overflow-visible')
      expect(panel.parentElement).not.toHaveClass('overflow-hidden')
    })
  })
})
