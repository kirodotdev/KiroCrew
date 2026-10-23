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
 *
 * Also Keep side panel open (`dashboard.side_panel_always_open`), the other
 * setting that opens the panel on its own: on for the full chat page only, off
 * on a phone and in an embedded companion chat.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, renderHook, act, waitFor, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createConnectedTestStore, createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { __resetPanelTabs, usePanelTabs, takePlainPanelShow } from '../hooks/usePanelTabs'
import { setSidePanelAlwaysOpen, switchSlot, toggleActivity } from '../store/chatSlice'
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
  sidePanelFillWidth: () => undefined,
}))

// --- Stub hooks ---
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
let mockIsMobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mockIsMobile }))

// --- Stub API. `dashboardConfig` and `projectGit` are the two the gate reads. ---
let autoOpenGitPanel = false
let sidePanelAlwaysOpen = false
/** Delay before `dashboardConfig` resolves, so a test can make the git query win
 *  the race and prove the marker is not consumed while the flag is unknown. */
let dashboardConfigDelayMs = 0
/** Make `dashboardConfig` reject, as it does while the gateway restarts. The
 *  test QueryClient does not retry, so one rejection fails the read. */
let dashboardConfigFails = false
vi.mock('../api/client', () => ({
  api: {
    ...Object.fromEntries(
      ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
       'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
       'chatSlotWorkspace', 'models', 'planFromChat', 'renameSlot',
       'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
       'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
       'notifications', 'status', 'generateTitle'].map(k => [k, vi.fn().mockResolvedValue(
        k === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {}
      )]),
    ),
    dashboardConfig: vi.fn().mockImplementation(() => new Promise((resolve, reject) => {
      if (dashboardConfigFails) { reject(new Error('dashboard config unavailable')); return }
      const value = { auto_open_git_panel: autoOpenGitPanel, side_panel_always_open: sidePanelAlwaysOpen }
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
import { ArtifactChatPanel } from '../components/ArtifactChatPanel'

const SLOT = 'chat-git-1'
const PROJECT = '/home/user/repo'
/** An artifact's companion chat: the artifact page's column switches to it. */
const COMPANION = 'chat-artifact-1'
/** A plain chat that a switch off the chat page enters, as the Projects page does. */
const OTHER = 'chat-plain-1'

/** Store with one active, EMPTY slot whose project dir is a git repo — the exact
 *  shape a freshly created chat has. `otherKeys` adds plain chats to the list,
 *  none of them active.
 *
 *  Built by PATCHING each slice's real initial state rather than substituting a
 *  hand-written object: ChatPage reads keyed sub-maps (`slotContextPct[...]`),
 *  so a partial slice throws before the behaviour under test can run. */
function storeWithGitSlot(otherKeys: string[] = []) {
  const init = createTestStore().getState()
  return createTestStore({
    dashboard: {
      ...init.dashboard,
      slots: [
        { key: SLOT, title: 'New Session…', messages: 0, project: PROJECT },
        ...otherKeys.map(key => ({ key, title: key, messages: 0 })),
      ],
    } as unknown as RootState['dashboard'],
    chat: { ...init.chat, activeSlot: SLOT, activityTab: 'files' } as RootState['chat'],
  })
}

/** `storeWithGitSlot` plus an artifact's companion chat, connected: the chat
 *  page acts on a `?sid=` link only while the dashboard is online. */
function storeWithCompanionSlot() {
  const init = createTestStore().getState()
  return createConnectedTestStore({
    dashboard: {
      slots: [
        { key: SLOT, title: 'New Session…', messages: 0, project: PROJECT },
        { key: COMPANION, title: 'Artifact chat', messages: 0, artifact: 'doc' },
      ],
    } as unknown as RootState['dashboard'],
    chat: { ...init.chat, activeSlot: SLOT, activityTab: 'files' } as RootState['chat'],
  })
}

/** `queryClient`: pass the one a previous render used to mount the page again
 *  on its warm cache, as a return from another route does. `at`: the URL and
 *  route the page mounts on (a popout lives on `/popout/chat`). */
function renderChat(store = storeWithGitSlot(), page = <ChatPage />, queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } }), at = { entry: '/chat', path: '/chat/:slug?' }) {
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={[at.entry]}>
            <Routes><Route path={at.path} element={page} /></Routes>
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
    dashboardConfigFails = false
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

describe('ChatPage — Keep side panel open', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 1400 })
    mockIsMobile = false
    autoOpenGitPanel = false
    sidePanelAlwaysOpen = true
    dashboardConfigDelayMs = 0
    dashboardConfigFails = false
    localStorage.clear()
    __resetPanelTabs()
  })
  afterEach(() => {
    sidePanelAlwaysOpen = false
    document.getElementById('activity-bar-slot')?.remove()
  })

  it('opens the panel for the chat on screen, saving nothing as that chat\'s choice', async () => {
    const { store } = renderChat()
    await waitFor(() => { expect(store.getState().chat.activityOpen).toBe(true) })
    expect(store.getState().chat.sidePanelAlwaysOpen).toBe(true)
    expect(localStorage.getItem(`mc-activity-open:${SLOT}`)).toBeNull()
  })

  it('opens the panel when the config read answers after the chat is shown', async () => {
    dashboardConfigDelayMs = 150
    const { store, queryClient } = renderChat()
    expect(store.getState().chat.activityOpen).toBe(false)
    await waitFor(() => { expect(store.getState().chat.activityOpen).toBe(true) })
    expect(queryClient.getQueryData<{ side_panel_always_open?: boolean }>(['dashboardConfig'])?.side_panel_always_open).toBe(true)
  })

  it('keeps a panel the user closed before the config read answered closed', async () => {
    dashboardConfigDelayMs = 150
    const store = storeWithGitSlot()
    const { queryClient } = renderChat(store)
    // The chat came back with its panel open; the user closes it while the
    // config read is still in flight.
    act(() => { store.dispatch(toggleActivity()) })
    act(() => { store.dispatch(toggleActivity()) })
    expect(store.getState().chat.activityOpen).toBe(false)
    await waitFor(() => {
      expect(queryClient.getQueryData<{ side_panel_always_open?: boolean }>(['dashboardConfig'])?.side_panel_always_open).toBe(true)
    })
    await waitFor(() => { expect(store.getState().chat.sidePanelAlwaysOpen).toBe(true) })
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  /** Mount the chat page on a first config read that fails, as a reload during
   *  a gateway restart does, and wait until the page has seen the failure:
   *  the Git tab's flag counts a failed read as known, so the tab appears. */
  async function renderOnFailedConfigRead() {
    dashboardConfigFails = true
    const panelTabs = renderHook(() => usePanelTabs(SLOT))
    const utils = renderChat()
    await waitFor(() => { expect(panelTabs.result.current.tabs.map(t => t.id)).toContain('git') })
    expect(utils.queryClient.getQueryState(['dashboardConfig'])?.status).toBe('error')
    return utils
  }

  /** The gateway is back: the next config read answers with the setting on. */
  async function refetchConfig(queryClient: QueryClient) {
    dashboardConfigFails = false
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['dashboardConfig'] }) })
  }

  it('keeps a panel the user closed after a failed config read closed when a refetch answers', async () => {
    const { store, queryClient } = await renderOnFailedConfigRead()
    // The user opens the panel and closes it again before the config is read.
    act(() => { store.dispatch(toggleActivity()) })
    act(() => { store.dispatch(toggleActivity()) })
    expect(store.getState().chat.activityOpen).toBe(false)
    await refetchConfig(queryClient)
    await waitFor(() => { expect(store.getState().chat.sidePanelAlwaysOpen).toBe(true) })
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('opens the chat on screen when a refetch answers after a failed config read', async () => {
    const { store, queryClient } = await renderOnFailedConfigRead()
    // The failed read reported nothing: the store has no copy of the setting.
    expect(store.getState().chat.sidePanelAlwaysOpenSetting).toBeNull()
    expect(store.getState().chat.activityOpen).toBe(false)
    await refetchConfig(queryClient)
    await waitFor(() => { expect(store.getState().chat.activityOpen).toBe(true) })
    expect(store.getState().chat.sidePanelAlwaysOpen).toBe(true)
    expect(localStorage.getItem(`mc-activity-open:${SLOT}`)).toBeNull()
  })

  it('opens the chat on screen when the setting turned on while the chat page was away', async () => {
    // Settings is a sibling route: the chat page is unmounted while the save
    // writes the new value into the config cache, and it mounts again on the
    // same chat with that cache warm.
    sidePanelAlwaysOpen = false
    const store = storeWithGitSlot()
    const away = renderChat(store)
    await waitFor(() => {
      expect(away.queryClient.getQueryData<{ side_panel_always_open?: boolean }>(['dashboardConfig'])?.side_panel_always_open).toBe(false)
    })
    away.unmount()
    sidePanelAlwaysOpen = true
    away.queryClient.setQueryData(['dashboardConfig'], { auto_open_git_panel: false, side_panel_always_open: true })
    renderChat(store, <ChatPage />, away.queryClient)
    await waitFor(() => { expect(store.getState().chat.activityOpen).toBe(true) })
    expect(localStorage.getItem(`mc-activity-open:${SLOT}`)).toBeNull()
  })

  it('keeps that turn-on for the chat page when an embedded chat mounts first', async () => {
    // The way back from Settings passes a page with an embedded companion
    // chat, which mounts on the warm config cache the save left. Its column
    // never shows the panel, so the chat page must still see the turn-on when
    // it mounts after it.
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    queryClient.setQueryData(['dashboardConfig'], { auto_open_git_panel: false, side_panel_always_open: true })
    const store = storeWithGitSlot()
    const embed = renderChat(store, <ChatPage embedded embedMode="chat" />, queryClient)
    expect(store.getState().chat.activityOpen).toBe(false)
    embed.unmount()
    renderChat(store, <ChatPage />, queryClient)
    await waitFor(() => { expect(store.getState().chat.activityOpen).toBe(true) })
  })

  it('opens the chat on screen when the setting turns on while the chat page is up', async () => {
    sidePanelAlwaysOpen = false
    const { store, queryClient } = renderChat()
    await waitFor(() => {
      expect(queryClient.getQueryData<{ side_panel_always_open?: boolean }>(['dashboardConfig'])?.side_panel_always_open).toBe(false)
    })
    expect(store.getState().chat.activityOpen).toBe(false)
    sidePanelAlwaysOpen = true
    act(() => { queryClient.setQueryData(['dashboardConfig'], { auto_open_git_panel: false, side_panel_always_open: true }) })
    await waitFor(() => { expect(store.getState().chat.activityOpen).toBe(true) })
  })

  it('keeps a panel the user closed shut when the chat page comes back with no setting change', async () => {
    const store = storeWithGitSlot()
    const first = renderChat(store)
    await waitFor(() => { expect(store.getState().chat.activityOpen).toBe(true) })
    act(() => { store.dispatch(toggleActivity()) })
    first.unmount()
    renderChat(store, <ChatPage />, first.queryClient)
    // The config read is the hook's only async input: once it is settled, no
    // late value can still open the panel.
    await waitFor(() => { expect(first.queryClient.isFetching({ queryKey: ['dashboardConfig'] })).toBe(0) })
    expect(store.getState().chat.sidePanelAlwaysOpen).toBe(true)
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('stays off on a phone, where the panel covers the chat', async () => {
    mockIsMobile = true
    const { store, queryClient } = renderChat()
    // The config read has landed with the setting ON, so the off below is the
    // phone guard and not a config that is still unknown.
    await waitFor(() => {
      expect(queryClient.getQueryData<{ side_panel_always_open?: boolean }>(['dashboardConfig'])?.side_panel_always_open).toBe(true)
    })
    expect(store.getState().chat.sidePanelAlwaysOpen).toBe(false)
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('is turned off by an embedded companion chat', async () => {
    const store = storeWithGitSlot()
    store.dispatch(setSidePanelAlwaysOpen({ on: true, setting: true }))
    // On its own route: an embed rewrites its URL to /embed/chat, and it
    // reports the setting only once the config read has returned data.
    renderChat(store, <ChatPage embedded embedMode="chat" />, undefined,
      { entry: `/embed/chat?sid=${SLOT}`, path: '/embed/chat/:slug?' })
    await waitFor(() => { expect(store.getState().chat.sidePanelAlwaysOpen).toBe(false) })
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('reopens the panel it opened when the chat page comes back after an embedded companion chat', async () => {
    const store = storeWithGitSlot()
    const first = renderChat(store)
    await waitFor(() => { expect(store.getState().chat.activityOpen).toBe(true) })
    first.unmount()
    // A page with a companion column holds the setting off, closing the panel.
    const embed = renderChat(store, <ChatPage embedded embedMode="chat" />, first.queryClient)
    await waitFor(() => { expect(store.getState().chat.sidePanelAlwaysOpen).toBe(false) })
    expect(store.getState().chat.activityOpen).toBe(false)
    embed.unmount()
    renderChat(store, <ChatPage />, first.queryClient)
    await waitFor(() => { expect(store.getState().chat.activityOpen).toBe(true) })
    expect(store.getState().chat.activeSlot).toBe(SLOT)
    expect(localStorage.getItem(`mc-activity-open:${SLOT}`)).toBeNull()
  })

  it('reopens the chat page\'s panel after an artifact companion switched to its own chat', async () => {
    // The artifact page's companion column switches the store to its own chat
    // and mounts an embedded chat page, which holds the setting off. Back on
    // `/chat?sid=`, the page switches chats before it lifts the hold.
    const atChat = { entry: `/chat?sid=${SLOT}`, path: '/chat/:slug?' }
    const store = storeWithCompanionSlot()
    const first = renderChat(store, <ChatPage />, undefined, atChat)
    await waitFor(() => { expect(store.getState().chat.activityOpen).toBe(true) })
    first.unmount()
    const companion = renderChat(store,
      <ArtifactChatPanel slotKey={COMPANION} creating={false} onNewChat={() => {}} onOpenFull={() => {}} onClose={() => {}} />,
      first.queryClient, { entry: '/artifacts/doc', path: '/artifacts/:slug' })
    // The companion's switch has landed, with the setting held off.
    await waitFor(() => {
      const chat = store.getState().chat
      expect([chat.activeSlot, chat.slotSwitchTarget, chat.sidePanelAlwaysOpen]).toEqual([COMPANION, null, false])
    })
    expect(store.getState().chat.activityOpen).toBe(false)
    companion.unmount()
    renderChat(store, <ChatPage />, first.queryClient, atChat)
    // The page's switch to the linked chat has landed, and so has the lift.
    await waitFor(() => {
      const chat = store.getState().chat
      expect([chat.activeSlot, chat.slotSwitchTarget, chat.sidePanelAlwaysOpen]).toEqual([SLOT, null, true])
    })
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(localStorage.getItem(`mc-activity-open:${SLOT}`)).toBeNull()
  })

  it('opens the panel in a popped-out chat window, which renders embedded', async () => {
    const { store } = renderChat(storeWithGitSlot(), <ChatPage embedded embedMode="chat" popout />, undefined,
      { entry: `/popout/chat?sid=${SLOT}`, path: '/popout/chat/:slug?' })
    await waitFor(() => { expect(store.getState().chat.activityOpen).toBe(true) })
    expect(store.getState().chat.sidePanelAlwaysOpen).toBe(true)
    expect(localStorage.getItem(`mc-activity-open:${SLOT}`)).toBeNull()
  })

  it('ends a panel show when the panel closes, which re-arms Pin Summary', async () => {
    const { store } = renderChat()
    await waitFor(() => { expect(store.getState().chat.activityOpen).toBe(true) })
    // The show's landing, as the side panel takes it.
    expect(takePlainPanelShow(SLOT)).toBe(true)
    expect(takePlainPanelShow(SLOT)).toBe(false)
    act(() => { store.dispatch(toggleActivity()) })
    expect(takePlainPanelShow(SLOT)).toBe(true)
  })

  it('ends a panel show when the chat changes while the chat page is away', async () => {
    // The Projects page, a notification or the command palette switches chats
    // while no chat page is mounted, and the page mounts again on the other
    // chat. The first chat's show has ended, so going back to it is a new show.
    const store = storeWithGitSlot([OTHER])
    const first = renderChat(store)
    await waitFor(() => { expect(store.getState().chat.activityOpen).toBe(true) })
    expect(takePlainPanelShow(SLOT)).toBe(true)
    first.unmount()
    await store.dispatch(switchSlot(OTHER))
    renderChat(store, <ChatPage />, first.queryClient)
    expect([store.getState().chat.activeSlot, store.getState().chat.activityOpen]).toEqual([OTHER, true])
    await act(async () => { await store.dispatch(switchSlot(SLOT)) })
    await waitFor(() => {
      const chat = store.getState().chat
      expect([chat.activeSlot, chat.slotSwitchTarget, chat.activityOpen]).toEqual([SLOT, null, true])
    })
    expect(takePlainPanelShow(SLOT)).toBe(true)
  })

  it('keeps a panel show when the chat page comes back on the same chat', async () => {
    // A return from Settings: no chat changed while the page was away, so the
    // show goes on and lands nothing.
    const store = storeWithGitSlot()
    const first = renderChat(store)
    await waitFor(() => { expect(store.getState().chat.activityOpen).toBe(true) })
    expect(takePlainPanelShow(SLOT)).toBe(true)
    first.unmount()
    renderChat(store, <ChatPage />, first.queryClient)
    // The config read is the only async input that can open or close the panel
    // here: once it is settled, no late report can end the show.
    await waitFor(() => { expect(first.queryClient.isFetching({ queryKey: ['dashboardConfig'] })).toBe(0) })
    expect([store.getState().chat.activeSlot, store.getState().chat.activityOpen]).toEqual([SLOT, true])
    expect(takePlainPanelShow(SLOT)).toBe(false)
  })

  it('creates the Git tab as an automatic open, so it does not stop Pin Summary', async () => {
    const panelTabs = renderHook(() => usePanelTabs(SLOT))
    renderChat()
    await waitFor(() => { expect(panelTabs.result.current.tabs.map(t => t.id)).toContain('git') })
    expect(takePlainPanelShow(SLOT)).toBe(true)
  })
})

describe('ChatPage — mobile sessions FAB vs the side panel close button', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 390 })
    mockIsMobile = true
    autoOpenGitPanel = false
    dashboardConfigDelayMs = 0
    dashboardConfigFails = false
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
