/**
 * Tests for the Git side-panel auto-open gate and the mobile sessions FAB that
 * used to sit on top of the panel's own close button.
 *
 * Two behaviours, one root cause: on a project that is a git repo the panel used
 * to force itself open for EVERY new chat (a new slot inherits
 * `dashboard.default_project`, so the "once per slot+path" marker never
 * suppresses it), and on mobile the panel covers the whole content area — where
 * the `fixed` sessions FAB paints over its collapse button and strands the user
 * inside a panel they cannot close.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, renderHook, act, waitFor, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { __resetPanelTabs, usePanelTabs } from '../hooks/usePanelTabs'
import { toggleActivity } from '../store/chatSlice'
import type { RootState } from '../store'

// --- Stub child components (same scaffold as ChatPage.responsivePanel test) ---
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
  SIDE_PANEL_MIN_W: 320,
  SIDE_PANEL_RESERVED_W: 560,
  CHAT_PANE_MIN_W: 320,
  measureSidePanelReservedW: () => 560,
  sidePanelFillWidth: () => undefined,
}))

// --- Stub hooks ---
vi.mock('../hooks/usePanelState', () => ({ usePanelState: () => ({ isOpen: false, openPanel: vi.fn(), closePanel: vi.fn() }), useDiffPanel: () => ({ isOpen: false, filePath: '', original: '', modified: '', openDiff: vi.fn(), closeDiff: vi.fn() }) }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
let mockIsMobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mockIsMobile }))

// --- Stub API. `dashboardConfig` and `projectGit` are the two the gate reads. ---
let autoOpenGitPanel = false
/** Delay before `dashboardConfig` resolves, so a test can make the git query win
 *  the race and prove the marker is not consumed while the flag is unknown. */
let dashboardConfigDelayMs = 0
vi.mock('../api/client', () => ({
  api: {
    ...Object.fromEntries(
      ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
       'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
       'chatSlotWorkspace', 'models', 'planAction', 'planFromChat', 'renameSlot',
       'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
       'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
       'notifications', 'status', 'generateTitle'].map(k => [k, vi.fn().mockResolvedValue(
        k === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {}
      )]),
    ),
    dashboardConfig: vi.fn().mockImplementation(() => new Promise(resolve => {
      const value = { auto_open_git_panel: autoOpenGitPanel }
      if (dashboardConfigDelayMs) setTimeout(() => resolve(value), dashboardConfigDelayMs)
      else resolve(value)
    })),
    projectGit: vi.fn().mockResolvedValue({ repo: true, branch: 'main' }),
  },
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
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import ChatPage from '../pages/ChatPage'

const SLOT = 'chat-git-1'
const PROJECT = '/home/user/repo'

/** Store with one active, EMPTY slot whose project dir is a git repo — the exact
 *  shape a freshly created chat has.
 *
 *  Built by PATCHING each slice's real initial state rather than substituting a
 *  hand-written object: ChatPage reads keyed sub-maps (`slotContextPct[...]`),
 *  so a partial slice throws before the behaviour under test can run. */
function storeWithGitSlot() {
  const init = createTestStore().getState()
  return createTestStore({
    dashboard: {
      ...init.dashboard,
      slots: [{ key: SLOT, title: 'New Session…', messages: 0, project: PROJECT }],
    } as unknown as RootState['dashboard'],
    chat: { ...init.chat, activeSlot: SLOT, activityTab: 'files' } as RootState['chat'],
  })
}

function renderChat(store = storeWithGitSlot()) {
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

describe('ChatPage — Git panel auto-open is opt-in', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 1400 })
    mockIsMobile = false
    autoOpenGitPanel = false
    dashboardConfigDelayMs = 0
    localStorage.clear()
    __resetPanelTabs()
  })
  afterEach(() => { document.getElementById('activity-bar-slot')?.remove() })

  it('creates the Git tab but leaves the panel CLOSED by default', async () => {
    const panelTabs = renderHook(() => usePanelTabs(SLOT))
    const { store } = renderChat()

    // The tab is the discoverability affordance, and it is unconditional.
    await waitFor(() => {
      expect(panelTabs.result.current.tabs.map(t => t.id)).toContain('git')
    })
    // Falsification: flipping the config below is what makes this true, so a
    // regression that re-adds the unconditional dispatch fails HERE.
    expect(store.getState().chat.activityOpen).toBe(false)
    expect(screen.queryByTestId('side-panel')).not.toBeInTheDocument()
  })

  it('opens the panel when dashboard.auto_open_git_panel is on', async () => {
    autoOpenGitPanel = true
    const { store } = renderChat()

    await waitFor(() => { expect(store.getState().chat.activityOpen).toBe(true) })
  })

  it('still opens when the git check wins the race against the config read', async () => {
    // The effect consumes a one-shot localStorage marker. If it acts while the
    // opt-in is still unknown, the marker is burned with the flag reading false
    // and an opted-in user never sees the panel — for the rest of that session,
    // because the marker persists.
    autoOpenGitPanel = true
    dashboardConfigDelayMs = 150
    const { store } = renderChat()

    // The Git tab is likewise deferred, not skipped.
    await waitFor(() => { expect(store.getState().chat.activityOpen).toBe(true) })
  })
})

describe('ChatPage — mobile sessions FAB vs the side panel close button', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 390 })
    mockIsMobile = true
    autoOpenGitPanel = false
    dashboardConfigDelayMs = 0
    localStorage.clear()
    __resetPanelTabs()
  })

  /** The FLOATING sessions opener, told apart from the identically-labelled
   *  control in the session header by its `fixed` wrapper — that wrapper is the
   *  whole reason this bug existed. */
  const fab = () => screen.queryAllByLabelText('Toggle sessions')
    .find(el => el.parentElement?.classList.contains('fixed')) ?? null

  it('shows the FAB on an empty chat while the panel is closed', async () => {
    renderChat()
    await waitFor(() => { expect(fab()).not.toBeNull() })
  })

  it('hides the FAB while the inline panel covers the content area', async () => {
    const { store } = renderChat()
    await waitFor(() => { expect(fab()).not.toBeNull() })

    act(() => { store.dispatch(toggleActivity()) })

    // The panel is inline on mobile (no actbar column) and full width, so the
    // `fixed` FAB would land on its collapse button — with a higher z-index.
    expect(await screen.findByTestId('side-panel')).toBeInTheDocument()
    expect(fab()).toBeNull()

    // Closing the panel brings it back: this hides the FAB, it does not retire it.
    act(() => { store.dispatch(toggleActivity()) })
    await waitFor(() => { expect(fab()).not.toBeNull() })
  })
})
