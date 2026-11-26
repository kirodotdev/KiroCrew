import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { act, render, renderHook, screen, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { namedCeiling } from './namedCeiling'

/**
 * A browser that blocks site data makes the `localStorage` and `sessionStorage`
 * ACCESSORS throw `SecurityError`, so even a `typeof localStorage` test throws.
 * A reader that runs while its module loads then fails the dashboard's
 * first-load chunk before React mounts, and the boot panel says the dashboard
 * did not load (#42345). Each reader below runs while the first page loads (at
 * module load, in a first-render initializer, or once the session list
 * arrives); each must fall back to its default instead.
 */
const STORES = ['localStorage', 'sessionStorage'] as const

/** A page module stood in for by a marker, so a mount test exercises the shell around it. */
const stub = (testId: string) => ({ default: () => createElement('div', { 'data-testid': testId }) })

/** Module stand-ins a mount below registers; each test's own list is undone after it. */
let mocked: string[] = []
function mockModule(path: string, factory: () => unknown) {
  vi.doMock(path, factory)
  mocked.push(path)
}

/** The neighbours a dashboard mount needs and a unit test has no use for: the
 *  network, the live socket, and the surfaces that need a real layout engine. */
function mockMountNeighbours() {
  mockModule('../api/client', () => ({
    api: new Proxy({}, {
      get: (_t, k) => vi.fn().mockResolvedValue(
        k === 'chatSlots' || k === 'approvals' ? []
          : k === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 }
            : k === 'listInstances' ? { instances: [], warm_set_cap: 5 }
              : {},
      ),
    }),
    isAuthBannerShown: vi.fn(() => false),
    ApiError: class ApiError extends Error {
      status: number
      constructor(status: number, message: string) {
        super(message)
        this.status = status
      }
    },
  }))
  mockModule('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {}, subscribeSubagents: () => {}, forceReconnect: () => {} }) }))
  mockModule('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' }) }))
  mockModule('../hooks/useDashboardHealthProbe', () => ({ useDashboardHealthProbe: () => {} }))
  mockModule('../components/MarkdownRenderer', () => ({ default: () => null, Lightbox: () => null }))
  mockModule('react-virtuoso', () => ({ Virtuoso: () => null }))
  vi.stubGlobal('matchMedia', (media: string) => ({
    matches: false, media, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  }))
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
}

/** The providers `main.tsx` mounts the dashboard under, around a route. */
async function providers(store: unknown, route: string) {
  const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
  const { Provider } = await import('react-redux')
  const { MemoryRouter } = await import('react-router-dom')
  const { ThemeProvider } = await import('../hooks/useTheme')
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client },
    createElement(Provider, { store: store as never },
      createElement(ThemeProvider, null, createElement(MemoryRouter, { initialEntries: [route] }, children))))
}

describe('storage reads at boot with site data blocked', () => {
  const saved = new Map<string, PropertyDescriptor | undefined>()

  beforeEach(() => {
    vi.resetModules()
    for (const name of STORES) {
      saved.set(name, Object.getOwnPropertyDescriptor(window, name))
      Object.defineProperty(window, name, {
        configurable: true,
        get() {
          throw new DOMException('Access is denied for this document.', 'SecurityError')
        },
      })
    }
  })

  afterEach(() => {
    for (const path of mocked) vi.doUnmock(path)
    mocked = []
    vi.unstubAllGlobals()
    for (const name of STORES) {
      const original = saved.get(name)
      if (original) Object.defineProperty(window, name, original)
      else Reflect.deleteProperty(window, name)
    }
  })

  it('loads the dock terminal with its default layout', async () => {
    const { useBottomTerminal } = await import('../hooks/useBottomTerminal')
    expect(renderHook(() => useBottomTerminal()).result.current).toEqual({
      open: false, fullscreen: false, height: 300, width: 420, position: 'bottom', tabs: [], activeId: null,
    })
  })

  it('loads the terminal font preference with its defaults', async () => {
    const font = await import('../hooks/useTerminalFont')
    expect(font.getTerminalFont()).toEqual({
      fontFamily: '',
      fontSize: font.DEFAULT_TERMINAL_FONT_SIZE,
      cursorStyle: font.DEFAULT_TERMINAL_CURSOR_STYLE,
      copyOnSelect: false,
      ctrlCopyPaste: false,
    })
  })

  it('loads the side panel tab strips empty', async () => {
    const { panelStripOf } = await import('../hooks/usePanelTabs')
    expect(panelStripOf('chat-1')).toEqual({ ids: [], activeId: null })
  })

  it('loads the chat state with no saved panel choices', async () => {
    const { default: chatReducer } = await import('../store/chatSlice')
    expect(chatReducer(undefined, { type: '@@init' }).slotActivity).toEqual({})
  })

  it('reads no saved pull request links for the chat page', async () => {
    const links = await import('../utils/pullRequestLinks')
    expect(links.loadSeenPullRequestLinks().size).toBe(0)
    expect(links.loadSourceSelections()).toEqual({})
    expect(links.loadRevealedSources()).toEqual({})
  })

  it('collects no orphaned keys once the session list loads', async () => {
    const { gcOrphanedStorage } = await import('../utils/storageGc')
    expect(gcOrphanedStorage(new Set(['chat-1']))).toBe(0)
  })

  it('reads the Mac chat-jump chord choice with its default', async () => {
    // Only a Mac reads it, so the shell mount below (not a Mac) never gets here.
    const platform = Object.getOwnPropertyDescriptor(navigator, 'platform')
    Object.defineProperty(navigator, 'platform', { value: 'MacIntel', configurable: true })
    try {
      const { IS_MAC, getCtrlDigitsEnabled } = await import('../hooks/useKeyboardShortcuts')
      expect(IS_MAC).toBe(true)
      expect(getCtrlDigitsEnabled()).toBe(true)
    } finally {
      if (platform) Object.defineProperty(navigator, 'platform', platform)
      else Reflect.deleteProperty(navigator, 'platform')
    }
  })

  it('skips the git panel auto-open for a chat in a git project', async () => {
    const { usePaneLayoutState } = await import('../pages/chat/page/paneLayout')
    const openView = vi.fn()
    const noop = vi.fn()
    renderHook(() => usePaneLayoutState({
      dispatch: noop as never, slots: [], activeSlot: 'slot-a', autoOpenGitPanel: true, autoOpenGitPanelKnown: true,
      setSplitMode: noop, splitMode: false, setSplitAnchor: noop, tabsCtl: { openView, boardFirst: false } as never,
      splitFeatureEnabled: false, _slotProject: '/work/repo', projectGit: { repo: 'repo' } as never, projectGitError: false,
      isMobile: false, sidePanelAlwaysOpen: false, sidePanelAlwaysOpenKnown: false, activityOpen: false,
    }))
    // No marker can be stored, so opening it would reopen on every render.
    expect(openView).not.toHaveBeenCalled()
  })

  // The dashboard's whole module graph loads fresh inside this test (the storage
  // getters must throw while it loads), which is ~9s of transform and import on an
  // idle host; the explicit ceiling covers that work under a shared runner.
  it('mounts the app shell with its default theme, layout and shortcuts', { timeout: 45_000 }, async () => {
    // The shell's first render reads the theme, the first-run flags, the rail
    // and apps-list layout, the font, the metrics readout and the shortcut
    // switch; its mount effects read the last-seen version.
    mockMountNeighbours()
    mockModule('../pages/ChatPage', () => stub('chat-page'))
    mockModule('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
    const { createConnectedTestStore } = await import('./helpers')
    const { default: App } = await import('../App')
    const store = createConnectedTestStore({ dashboard: { status: { version: '1.2.3' } } } as never)
    const wrapper = await providers(store, '/chat')
    await act(async () => { render(createElement(wrapper, null, createElement(App))) })
    await act(async () => { await vi.dynamicImportSettled() })
    expect(await screen.findByTestId('chat-page', {}, namedCeiling('BLOCKED_STORAGE_SHELL_MOUNT', 10000))).toBeTruthy()
    expect(screen.queryByText(/went wrong/i)).toBeNull()
  })

  it('mounts the chat page and opens a chat with no saved layout, chat or prefill', async () => {
    // The page's first render reads the sessions-list pin and width; once the
    // list arrives it reads the chat to reopen, and the opened chat's composer
    // reads the prefill a launcher may have left.
    mockMountNeighbours()
    for (const path of ['../components/ChatInput', '../components/WelcomeView', '../components/MarkdownPanel',
      '../components/TypewriterText', '../components/OverlayDrawer', '../components/AgentDropdownList',
      '../components/ModelDropdownList', '../components/InfoTip', '../components/SegmentedControl',
      '../pages/chat/CollapsibleToolGroup', '../pages/chat/ActivityViewer', '../pages/chat/SessionColorPicker']) {
      mockModule(path, () => ({ default: () => null }))
    }
    mockModule('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
    mockModule('../pages/chat/SidePanel', () => ({
      default: () => null, SIDE_PANEL_MIN_W: 320, SIDE_PANEL_RESERVED_W: 560, CHAT_PANE_MIN_W: 320, sidePanelFillWidth: () => undefined,
    }))
    mockModule('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
    mockModule('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: () => {} }), voiceInputSupported: false }))
    mockModule('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
    const { createConnectedTestStore } = await import('./helpers')
    const { Routes, Route } = await import('react-router-dom')
    const { default: ChatPage } = await import('../pages/ChatPage')
    // The first pick waits for the edition lock (bounded by a timer); answer it
    // here so the pick runs at once.
    const { noteFeaturePreviewLockKnown } = await import('../utils/editionLocks')
    noteFeaturePreviewLockKnown()
    const store = createConnectedTestStore()
    const wrapper = await providers(store, '/chat')
    await act(async () => {
      render(createElement(wrapper, null, createElement(Routes, null, createElement(Route, { path: '/chat/:slug?', element: createElement(ChatPage) }))))
    })
    await act(async () => { await vi.dynamicImportSettled() })
    const slot = (key: string) => ({ key, title: key, messages: 2, running: false, mode: '', created: '', last_ts: '' })
    await act(async () => { store.dispatch({ type: 'dashboard/sseSlots', payload: [slot('slot-a'), slot('slot-b')] }) })
    await act(async () => { await vi.dynamicImportSettled() })
    expect(screen.queryByText(/went wrong/i)).toBeNull()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-a'), namedCeiling('BLOCKED_STORAGE_CHAT_AUTO_OPEN', 5000))
  })

  /** Mounts the theme provider with `/api/theme/boot` answering `boot` and no installed theme;
   *  collects every error the page reports as uncaught (a browser reports one a boundary caught too). */
  async function mountTheme(boot: object) {
    const errors: unknown[] = []
    const onError = (e: ErrorEvent) => { errors.push(e.error); e.preventDefault() }
    window.addEventListener('error', onError)
    onTestFinished(() => window.removeEventListener('error', onError))
    // happy-dom runs with error capture off, so a listener's throw leaves dispatchEvent instead
    // of reaching window `error`, and React's dev error replay then wedges every later render.
    const domTarget = Object.getPrototypeOf(Node.prototype) as EventTarget
    const dispatch = domTarget.dispatchEvent
    const reporting = vi.spyOn(domTarget, 'dispatchEvent').mockImplementation(function (this: EventTarget, event: Event) {
      try {
        return dispatch.call(this, event)
      } catch (error) {
        if (this === window) throw error
        return window.dispatchEvent(new ErrorEvent('error', { error }))
      }
    })
    onTestFinished(() => reporting.mockRestore())
    mockModule('../api/client', () => ({
      api: new Proxy({}, {
        get: (_t, k) => k === 'themeDetail' ? vi.fn().mockRejectedValue(new Error('no such theme'))
          : vi.fn().mockResolvedValue(k === 'themeBoot' ? boot : k === 'themes' ? { themes: [] } : {}),
      }),
    }))
    vi.stubGlobal('matchMedia', (media: string) => ({
      matches: false, media, onchange: null,
      addListener: () => {}, removeListener: () => {},
      addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
    }))
    const { useTheme } = await import('../hooks/useTheme')
    const { createTestStore } = await import('./helpers')
    const { default: ErrorBoundary } = await import('../components/ErrorBoundary')
    let theme: ReturnType<typeof useTheme> | null = null
    const Probe = () => { theme = useTheme(); return createElement('div', { 'data-testid': 'theme-probe' }) }
    const wrapper = await providers(createTestStore(), '/')
    await act(async () => { render(createElement(ErrorBoundary, null, createElement(wrapper, null, createElement(Probe)))) })
    return { theme: () => theme!, errors }
  }

  it('boots the theme for a workspace that has not finished onboarding', async () => {
    const { theme, errors } = await mountTheme({ mode: 'system', color: 'kiro', onboarded: false, import_onboarded: false })
    await waitFor(() => expect(theme().themeBootReady).toBe(true), namedCeiling('BLOCKED_STORAGE_THEME_BOOT', 5000))
    await act(async () => { await Promise.resolve() })
    expect(theme().onboarded).toBe(false)
    expect(theme().importOnboarded).toBe(false)
    expect(errors).toEqual([])
    expect(screen.queryByText(/went wrong/i)).toBeNull()
    expect(screen.queryByTestId('theme-probe')).toBeTruthy()
  })

  it('falls back to the default look when the saved theme is gone', async () => {
    const { theme, errors } = await mountTheme({ mode: 'dark', color: 'custom-gone-42345', onboarded: true, import_onboarded: true })
    const { DEFAULT_COLOR_THEME } = await import('../hooks/useTheme')
    await waitFor(() => expect(theme().customThemesLoaded).toBe(true), namedCeiling('BLOCKED_STORAGE_THEME_CATALOG', 5000))
    await waitFor(() => {
      expect(errors).toEqual([])
      expect(theme().colorTheme).toBe(DEFAULT_COLOR_THEME)
    }, namedCeiling('BLOCKED_STORAGE_THEME_REPAIR', 5000))
    // The repair resets only the gone theme: the workspace's dark appearance stays.
    expect(theme().preference).toBe('dark')
    expect(screen.queryByText(/went wrong/i)).toBeNull()
    expect(screen.queryByTestId('theme-probe')).toBeTruthy()
  })

  it('keeps the other look choice when one is picked', async () => {
    const { theme, errors } = await mountTheme({ mode: 'system', color: 'kiro', onboarded: true, import_onboarded: true })
    await waitFor(() => expect(theme().customThemesLoaded).toBe(true), namedCeiling('BLOCKED_STORAGE_THEME_CATALOG', 5000))
    await act(async () => { theme().setColorTheme('nord') })
    await act(async () => { theme().setTheme('dark') })
    expect(theme().colorTheme).toBe('nord')
    expect(theme().preference).toBe('dark')
    await act(async () => { theme().setColorTheme('dracula') })
    expect(theme().colorTheme).toBe('dracula')
    expect(theme().preference).toBe('dark')
    expect(errors).toEqual([])
  })

  it('renders the composer with its default height, collapse and voice mode', async () => {
    const { useComposerCollapse } = await import('../components/chat-input/collapse')
    const { useManualHeight } = await import('../components/chat-input/sizing')
    const { useHoldToTalk } = await import('../components/chat-input/voice')
    const { createTestStore } = await import('./helpers')
    const { Provider } = await import('react-redux')
    const store = createTestStore()
    const wrapper = ({ children }: { children: ReactNode }) => createElement(Provider, { store }, children)
    expect(renderHook(() => useComposerCollapse({ collapsible: true, composerControl: () => null, value: '' })).result.current.composerCollapsed).toBe(false)
    expect(renderHook(() => useManualHeight({ wrapperRef: { current: null }, pendingFilesCount: 0, pendingSessionsCount: 0 })).result.current.manualHeight).toBeNull()
    const voice = renderHook(() => useHoldToTalk({
      onVoiceStart: () => {}, onVoiceStop: () => {}, onVoiceCancel: () => {},
      voiceCaptureActive: false, voiceRecording: false, voiceTranscribeActive: false, voiceTranscribing: false,
      voiceDownload: undefined, voiceBusyElsewhere: false, voiceBusyElsewhereSession: undefined,
      composerHasDraft: false, disabled: false, optimizing: false, placeholder: '',
    }), { wrapper })
    expect(voice.result.current).toBeTruthy()
  })
})

describe('storage reads at boot with site data allowed', () => {
  beforeEach(() => vi.resetModules())

  it('still restores the saved terminal layout and font', async () => {
    localStorage.setItem('mc-bottom-terminal', JSON.stringify({ height: 400, position: 'right', tabs: [{ id: 't1', name: 'one' }], activeId: 't1' }))
    localStorage.setItem('mc-terminal-font', JSON.stringify({ fontSize: 17 }))
    const { useBottomTerminal } = await import('../hooks/useBottomTerminal')
    const { getTerminalFont } = await import('../hooks/useTerminalFont')
    const layout = renderHook(() => useBottomTerminal()).result.current
    expect(layout).toMatchObject({ height: 400, position: 'right', activeId: 't1' })
    expect(layout.tabs.map(t => t.id)).toEqual(['t1'])
    expect(getTerminalFont().fontSize).toBe(17)
  })
})
