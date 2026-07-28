/**
 * Tests for ChatPage's responsive activity panel behaviors:
 * - auto-collapse when the window shrinks below the panel's space threshold
 * - auto-reopen (with hysteresis) when space returns — only if it was the
 *   auto-collapse that closed it
 * - a manual toggle cancels any pending auto-reopen (review round 12 fix)
 * - portal slot self-healing: if the actbar slot div isn't in the DOM when
 *   ChatPage looks for it, a MutationObserver latches it when it appears
 *   (fixes the mobile->desktop race that stranded the panel inline).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, renderHook, act, waitFor, screen, fireEvent } from '@testing-library/react'
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
  default: ({ fillWidth }: { fillWidth?: number }) => (
    <div data-testid="side-panel" data-fill-width={fillWidth ?? ''} />
  ),
  SIDE_PANEL_MIN_W: 320,
  SIDE_PANEL_RESERVED_W: 560,
  CHAT_PANE_MIN_W: 320,
  measureSidePanelReservedW: () => 560,
  // Real arithmetic (the gate under test lives here); only the component is stubbed.
  sidePanelFillWidth: ({ winW, railW, sidebarW, isMobile }: { winW: number; railW: number; sidebarW: number; isMobile: boolean }) => {
    if (isMobile) return Math.max(320, winW)
    const avail = winW - railW - sidebarW
    return avail >= 640 ? undefined : Math.max(320, avail)
  },
}))

// --- Stub hooks ---
vi.mock('../hooks/usePanelState', () => ({ usePanelState: () => ({ isOpen: false, openPanel: vi.fn(), closePanel: vi.fn() }), useDiffPanel: () => ({ isOpen: false, filePath: '', original: '', modified: '', openDiff: vi.fn(), closeDiff: vi.fn() }) }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
// useIsMobile reads window.matchMedia at MODULE load, so per-test matchMedia
// stubs can't move it. Mock the hook with a mutable flag instead: desktop
// (false) by default, flipped by the mobile describe below.
let mockIsMobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mockIsMobile }))

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

describe('ChatPage — activity panel open state is resize-independent', () => {
  beforeEach(() => setWindowWidth(1400))
  afterEach(() => {
    document.getElementById('activity-bar-slot')?.remove()
  })

  it('stays open when the window shrinks below the old 880 space threshold', () => {
    const { store } = renderChat()
    act(() => { store.dispatch(toggleActivity()) })
    expect(store.getState().chat.activityOpen).toBe(true)

    resizeTo(850) // used to auto-collapse here
    expect(store.getState().chat.activityOpen).toBe(true)
    resizeTo(700) // and again below the mobile breakpoint
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('does not auto-reopen when space returns', () => {
    const { store } = renderChat()
    resizeTo(850)
    expect(store.getState().chat.activityOpen).toBe(false)

    resizeTo(1000)
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('does not undo a manual close on a later resize', () => {
    const { store } = renderChat()
    act(() => { store.dispatch(toggleActivity()) })
    act(() => { window.dispatchEvent(new CustomEvent('toggle-activity-panel')) })
    expect(store.getState().chat.activityOpen).toBe(false)

    resizeTo(850)
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

describe('ChatPage — session-header activity toggle (relocated from the top bar)', () => {
  beforeEach(() => { setWindowWidth(1400); localStorage.clear() })
  afterEach(() => { document.getElementById('activity-bar-slot')?.remove() })

  function renderWithSlot() {
    const store = createTestStore()
    act(() => {
      store.dispatch(switchSlot.pending('req-toggle', 'slot-toggle'))
      store.dispatch(switchSlot.fulfilled({
        key: 'slot-toggle',
        messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        running: false, hasMore: false, total: 1, queue: [],
      }, 'req-toggle', 'slot-toggle'))
    })
    return renderChat(store)
  }

  it('opens the activity panel from the session-header toggle', async () => {
    const { store } = renderWithSlot()
    const btn = await screen.findByLabelText('Open activity panel')
    expect(store.getState().chat.activityOpen).toBe(false)
    fireEvent.click(btn)
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('stays live in the 768-880 band that used to disable it, and opens beside the chat', async () => {
    const { store } = renderWithSlot()
    await screen.findByLabelText('Open activity panel')
    resizeTo(800) // below the old 880 space threshold (320 + 560), above mobile (768)

    const btn = await screen.findByLabelText('Open activity panel')
    expect(screen.queryByLabelText('Window too narrow for the activity panel')).not.toBeInTheDocument()
    fireEvent.click(btn)
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('stays live at 768 exactly (tablet portrait)', async () => {
    renderWithSlot()
    await screen.findByLabelText('Open activity panel')
    resizeTo(768)
    expect(await screen.findByLabelText('Open activity panel')).toBeInTheDocument()
  })

  it('opens BESIDE the chat on a wide window (no fill width)', async () => {
    // 1400 - rail 236 - sidebar 260 = 904 >= 640
    renderWithSlot()
    fireEvent.click(await screen.findByLabelText('Open activity panel'))
    expect(await screen.findByTestId('side-panel')).toHaveAttribute('data-fill-width', '')
  })

  it('opens FILLING the chat column when the rail + sidebar leave too little', async () => {
    renderWithSlot()
    resizeTo(800) // 800 - 236 - 260 = 304 < 640
    fireEvent.click(await screen.findByLabelText('Open activity panel'))
    expect(await screen.findByTestId('side-panel')).toHaveAttribute('data-fill-width', '320')
  })

  it('switches an already-open panel from beside to fill on resize, without remounting it', async () => {
    renderWithSlot()
    fireEvent.click(await screen.findByLabelText('Open activity panel'))
    const before = await screen.findByTestId('side-panel')
    expect(before).toHaveAttribute('data-fill-width', '')

    resizeTo(800)
    const after = await screen.findByTestId('side-panel')
    expect(after).toHaveAttribute('data-fill-width', '320')
    // Same DOM node: the mode change must not tear the panel down (live PTYs).
    expect(after).toBe(before)
  })
})

/**
 * Mobile regression: the toggle used to be gated on `!isMobile`, so a phone had
 * NO way to open the activity panel even though SidePanel already renders
 * full-width there and ChatPage keeps an inline (non-portal) render path
 * specifically for mobile.
 */
describe('ChatPage — activity toggle on mobile', () => {
  beforeEach(() => { mockIsMobile = true; setWindowWidth(390); localStorage.clear() })
  afterEach(() => { mockIsMobile = false })

  function renderWithSlot() {
    const store = createTestStore()
    act(() => {
      store.dispatch(switchSlot.pending('req-mob', 'slot-mob'))
      store.dispatch(switchSlot.fulfilled({
        key: 'slot-mob',
        messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        running: false, hasMore: false, total: 1, queue: [],
      }, 'req-mob', 'slot-mob'))
    })
    return renderChat(store)
  }

  it('renders the toggle at a phone viewport and opens the panel', async () => {
    const { store } = renderWithSlot()
    const btn = await screen.findByLabelText('Open activity panel')
    fireEvent.click(btn)
    expect(store.getState().chat.activityOpen).toBe(true)
    // Mobile has no actbar grid column, so the panel renders inline.
    expect(await screen.findByTestId('side-panel')).toBeInTheDocument()
  })

  it('does not disable the toggle at widths below the desktop space threshold', async () => {
    renderWithSlot()
    // 390px is far below 880, which would disable it on desktop.
    expect(await screen.findByLabelText('Open activity panel')).toBeInTheDocument()
    expect(screen.queryByLabelText('Window too narrow for the activity panel')).not.toBeInTheDocument()
  })
})

describe('ChatPage — message scroller contains its scroll', () => {
  beforeEach(() => setWindowWidth(1400))

  it('sets overscroll-behavior:contain so wheel deltas never chain to the document', async () => {
    const store = createTestStore()
    act(() => {
      store.dispatch(switchSlot.pending('req-scroll', 'slot-scroll'))
      store.dispatch(switchSlot.fulfilled({
        key: 'slot-scroll',
        messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        running: false, hasMore: false, total: 1, queue: [],
      }, 'req-scroll', 'slot-scroll'))
    })
    renderChat(store)

    const scroller = await screen.findByLabelText('Chat messages')
    // Positive control: the list really is the scroll container under test.
    expect(scroller.style.overflowY).toBe('auto')
    // The guard: without containment, a delta at the top or bottom edge
    // chains to the document and drags the whole app shell.
    expect(scroller.style.overscrollBehavior).toBe('contain')
  })
})
