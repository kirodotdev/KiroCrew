/**
 * Preview "focus" (expand) mode must not disable the sessions toggle.
 *
 * Entering focus mode (the Browser panel's expand button) hides the session
 * list to give the preview room, but that is a starting layout, not a lock:
 * the sessions toggle stays mounted and keeps working, and leaving focus mode
 * restores the pre-focus state only when the user did not override it.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { StrictMode } from 'react'
import { render, act, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { __resetPanelTabs } from '../hooks/usePanelTabs'
import { sseSlots } from '../store/dashboardSlice'
import { toggleActivity } from '../store/chatSlice'

vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../components/ChatInput', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
// The drawer stands in for `sidebarOpen` itself, so a test can assert the
// session list's real visibility rather than inferring it from the toggle label
// (which is not rendered at all on mobile, or with an empty list).
vi.mock('../components/OverlayDrawer', () => ({
  default: ({ open, children }: { open?: boolean; children?: React.ReactNode }) =>
    (open ? <div data-testid="sessions-drawer">{children}</div> : null),
}))
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
  // `expanded` is surfaced so a test can assert the panel stops claiming its
  // maximum once the user reopens the session list.
  default: ({ expanded }: { expanded?: boolean }) =>
    <div data-testid="side-panel" data-expanded={String(!!expanded)} />,
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
// useIsMobile reads matchMedia at module load, so a per-test matchMedia stub
// cannot move it. Mock the hook with a mutable flag instead.
let mockIsMobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mockIsMobile }))
vi.mock('../api/client', () => ({
  api: Object.fromEntries(
    ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
      'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
      'chatSlotWorkspace', 'models', 'planAction', 'planFromChat', 'renameSlot',
      'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
      'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
      'notifications', 'status', 'generateTitle'].map(k => [k, vi.fn().mockResolvedValue(
      k === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {},
    )]),
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
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as any
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as any

import ChatPage from '../pages/ChatPage'

const setFocus = (focused: boolean) => act(() => {
  window.dispatchEvent(new CustomEvent('kirocrew-preview-focus', { detail: { focused } }))
})

function renderChat({ slots = 1, strict = false }: { slots?: number; strict?: boolean } = {}) {
  const store = createTestStore()
  // The sessions toggle only renders when there is a session to show.
  act(() => {
    store.dispatch(sseSlots(
      Array.from({ length: slots }, (_, i) => ({ key: `slot-${i}`, title: `Session ${i}` }) as any),
    ))
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const tree = (
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
  render(strict ? <StrictMode>{tree}</StrictMode> : tree)
  return store
}

describe('ChatPage — sessions toggle inside preview focus mode', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 1400 })
    localStorage.clear()
    __resetPanelTabs()
  })

  it('keeps the toggle mounted and working while focus mode is active', () => {
    renderChat()
    expect(screen.getByRole('button', { name: 'Hide sessions sidebar' })).toBeInTheDocument()

    setFocus(true)
    // Focus mode hides the list, but the toggle is still there to bring it back.
    const toggle = screen.getByRole('button', { name: 'Show sessions sidebar' })

    fireEvent.click(toggle)
    expect(screen.getByRole('button', { name: 'Hide sessions sidebar' })).toBeInTheDocument()

    // Leaving focus mode must not undo that explicit choice.
    setFocus(false)
    expect(screen.getByRole('button', { name: 'Hide sessions sidebar' })).toBeInTheDocument()
  })

  it('restores the pre-focus session list when focus mode ends untouched', () => {
    renderChat()
    expect(screen.getByRole('button', { name: 'Hide sessions sidebar' })).toBeInTheDocument()

    setFocus(true)
    expect(screen.getByRole('button', { name: 'Show sessions sidebar' })).toBeInTheDocument()

    setFocus(false)
    expect(screen.getByRole('button', { name: 'Hide sessions sidebar' })).toBeInTheDocument()
    // The auto-hide is transient: only a user toggle writes the preference.
    expect(localStorage.getItem('mc-sidebar-pinned')).toBeNull()
  })

  it('restores the list under StrictMode, where a state updater runs twice', () => {
    // The restore value lives in a ref. Reading and clearing it inside a
    // setState updater would lose it here: StrictMode double-invokes the
    // updater, and the second pass would see an already-cleared ref.
    renderChat({ strict: true })
    expect(screen.getByRole('button', { name: 'Hide sessions sidebar' })).toBeInTheDocument()

    setFocus(true)
    expect(screen.getByRole('button', { name: 'Show sessions sidebar' })).toBeInTheDocument()

    setFocus(false)
    expect(screen.getByRole('button', { name: 'Hide sessions sidebar' })).toBeInTheDocument()
  })

  it('keeps the hover recents flyout available while focus mode hides the list', () => {
    renderChat()
    setFocus(true)
    // aria-haspopup mirrors flyoutEligible, which focus mode no longer suppresses.
    expect(screen.getByRole('button', { name: 'Show sessions sidebar' }))
      .toHaveAttribute('aria-haspopup', 'menu')
  })

  it('does not let focus mode overwrite the stored preference when the list drains to empty', () => {
    // The force-open rule persists 'true' whenever the list is empty and
    // unpinned. Focus mode also unpins, so without the guard, draining the list
    // to zero inside focus mode would write 'true' over a user who chose
    // hidden — and the restore on exit would then contradict it in the live state.
    const store = renderChat()
    fireEvent.click(screen.getByRole('button', { name: 'Hide sessions sidebar' }))
    expect(localStorage.getItem('mc-sidebar-pinned')).toBe('false')

    setFocus(true)
    act(() => { store.dispatch(sseSlots([])) })
    expect(localStorage.getItem('mc-sidebar-pinned')).toBe('false')

    setFocus(false)
    // Out of focus mode the empty list is the force-open rule's business again.
    expect(localStorage.getItem('mc-sidebar-pinned')).toBe('true')
  })

  it('stops maximizing the panel once the user reopens the session list', async () => {
    // The panel's maximum is measured against the header's reserve, which does
    // not know the session list's width — so holding it while the list is back
    // pushes the chat pane below its minimum and clips the transcript.
    const store = renderChat()
    act(() => { store.dispatch(toggleActivity()) })
    expect(await screen.findByTestId('side-panel')).toHaveAttribute('data-expanded', 'false')

    setFocus(true)
    expect(screen.getByTestId('side-panel')).toHaveAttribute('data-expanded', 'true')

    // The session list is the only term: the nav rail is narrow enough that the
    // header's own reserve already covers it, and it is not part of this
    // computation, so reopening the rail cannot cost the preview its maximum.
    fireEvent.click(screen.getByRole('button', { name: 'Show sessions sidebar' }))
    expect(screen.getByTestId('side-panel')).toHaveAttribute('data-expanded', 'false')
  })

  it('renders no sessions toggle when there is nothing in the list', () => {
    renderChat({ slots: 0 })
    expect(screen.queryByRole('button', { name: /sessions sidebar/ })).not.toBeInTheDocument()
  })

  it('closes the list in focus mode even with an empty session list', () => {
    // The toggle is unrendered either way here, so assert the list itself: the
    // force-open rule must not leave the preview covered by an undismissable list.
    renderChat({ slots: 0 })
    expect(screen.getByTestId('sessions-drawer')).toBeInTheDocument()

    setFocus(true)
    expect(screen.queryByTestId('sessions-drawer')).not.toBeInTheDocument()

    setFocus(false)
    expect(screen.getByTestId('sessions-drawer')).toBeInTheDocument()
  })
})

describe('ChatPage — mobile session drawer inside preview focus mode', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 420 })
    localStorage.clear()
    __resetPanelTabs()
    mockIsMobile = true
  })
  afterEach(() => { mockIsMobile = false })

  it('closes the open drawer on entry and leaves it reopenable', () => {
    // Mobile has its own state (`mobileSessions`) and no sessions toggle, so
    // focus mode closes the drawer outright instead of suppressing it.
    const openDrawer = () =>
      fireEvent.click(screen.getAllByRole('button', { name: 'Toggle sessions' })[0])
    renderChat()
    openDrawer()
    expect(screen.getByTestId('sessions-drawer')).toBeInTheDocument()

    setFocus(true)
    expect(screen.queryByTestId('sessions-drawer')).not.toBeInTheDocument()

    // Still reachable — an override would have made this button do nothing.
    openDrawer()
    expect(screen.getByTestId('sessions-drawer')).toBeInTheDocument()
  })
})
