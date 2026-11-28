/**
 * A relay Crew tab in the desktop app (#42922): whose top bar the window shows.
 *
 * Two pages take part, and each half is tested here against the whole <App />:
 *
 *  - The relay Crew's own page, in a pane. With the user-agent token
 *    `kiroCrewRelayPaneHeader` after `kiroCrewRelayPane` it draws its header in
 *    the pane form (no Crew switcher, never a window drag region). With the pane
 *    token alone (an older desktop app) it draws none, as before.
 *  - The desktop app's own page. Its bar stays whole until the shell says that
 *    the pane in front drew a header (a `header` pane event carrying `true`),
 *    then keeps the tab strip and the update pill only, and is whole again the
 *    moment the tab changes, the page reloads without a header, or the pane closes.
 *    The bell's button goes; the sheet's hook, the banner and the dock badge stay.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import type { QueryClient } from '@tanstack/react-query'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { renderWithProviders } from './helpers'
import { namedCeiling } from './namedCeiling'
import type { RootState } from '../store'
import { sseConnected } from '../store/dashboardSlice'
import { relayHeaderInFront, relayPaneOpened, setActiveId } from '../store/instancesSlice'
import { addNotification } from '../store/notificationsSlice'
import { dispatchLiveNotification } from '../hooks/notificationEvent'
import { NavigationLeaveGuardProvider } from '../components/NavigationLeaveGuard'
import { api } from '../api/client'
import type { RelayPaneEvent } from '../lib/relayCrewPanes'
import type { Notification } from '../types'
import App from '../App'
import en from '../i18n/locales/en.manual.json'

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))
// The tab strip, as a marker: what it lists is its own tests' subject
// (relayCrewPanes.test.tsx, TeamCrewsGroup.test.tsx). Here only whether the bar holds it.
vi.mock('../components/TeamCrewsGroup', () => ({ default: () => <div data-testid="crew-tab-strip" /> }))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { credits_used: 0, credits_covered: 3044, credits_plan: 10000, resets: '2026-07-01', plan: 'KIRO POWER', cost_usd: 0, overage_rate: '0.04' } }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
    themes: vi.fn().mockResolvedValue({ themes: [] }),
    members: vi.fn().mockResolvedValue({ members: [] }),
  },
  isAuthBannerShown: () => false,
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: query === '(prefers-color-scheme: dark)',
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })),
})
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const SHELL_SETTLED = namedCeiling('SHELL_SETTLED', 5000)
const shellSettled = (queryClient: QueryClient) =>
  waitFor(() => {
    expect(screen.getByTestId('dashboard-shell')).not.toHaveClass('animate-rise')
    expect(queryClient.isFetching()).toBe(0)
  }, SHELL_SETTLED)

const BASE_UA = 'Mozilla/5.0 (Macintosh) Electron/44'
const header = () => document.querySelector('header.topbar')
/** The top bar's bell button (the rail and the sheet carry the same word). */
const bell = () => document.querySelector<HTMLElement>('header.topbar [data-testid="notifications-bell-host"] button[aria-haspopup="dialog"]')
/** The readout capsule's connection dot: local gateway state, credits and resource use hang off it. */
const capsule = () => document.querySelector('.tb-capsule')
const search = () => document.querySelector('[data-topbar-overlay]')

afterEach(() => {
  delete (window as unknown as { relayCrewAPI?: unknown }).relayCrewAPI
  delete (window as unknown as { electronAPI?: unknown }).electronAPI
  vi.restoreAllMocks()
})

describe('the relay Crew\'s own page in a pane: which user agent draws a header', () => {
  it('draws its header in the pane form on the new token: no Crew switcher, never a drag region', async () => {
    vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(`${BASE_UA} kiroCrewRelayWindow kiroCrewRelayPane kiroCrewRelayPaneHeader`)
    const { store, queryClient } = renderWithProviders(<App />, { route: '/chat' })
    act(() => { store.dispatch(sseConnected()) })
    // What the desktop shell reads to confirm it (electron/relay-crew-window.js PANE_HEADER_READ).
    expect(document.querySelector('header[data-relay-pane-header]')).toBe(header())
    expect(header()).not.toBeNull()
    // The header row has its height back, and the header speaks the connection state itself.
    expect(screen.getByTestId('dashboard-shell').style.gridTemplateRows).toBe('')
    expect(screen.queryByTestId('relay-pane-gateway-status')).toBeNull()
    // Its own bell and readouts: the ones the window shows for this Crew.
    expect(bell()).not.toBeNull()
    expect(capsule()).not.toBeNull()
    expect(search()).not.toBeNull()
    expect(screen.getByTestId('focus-mode-toggle')).toBeInTheDocument()
    // The desktop app's tab strip sits one row above, so the page draws no switcher.
    await shellSettled(queryClient)
    expect(screen.queryByTestId('crew-tab-strip')).toBeNull()
    // Never a window drag region: no desktop shell class on this page, and a rule that
    // says so whatever class it carries.
    expect(screen.getByTestId('dashboard-shell').className).not.toMatch(/mac-electron|win-electron|linux-electron/)
    const css = readFileSync(resolve(__dirname, '../styles/shell-chrome.css'), 'utf8').replace(/\s+/g, '')
    expect(css).toContain('header.topbar-glass[data-relay-pane-header],header.topbar-glass[data-relay-pane-header]*{-webkit-app-region:no-drag;}')
    // ...and that rule's selector is this header's.
    expect((header() as HTMLElement).matches('header.topbar-glass[data-relay-pane-header]')).toBe(true)
    // The page is not the desktop app's: nothing of its bar is given up here.
    expect(header()).not.toHaveAttribute('data-relay-tab-strip-only')
  })

  it.each([
    ['an older desktop app (the pane token alone)', `${BASE_UA} kiroCrewRelayWindow kiroCrewRelayPane`],
    ['a token that only starts like the header token', `${BASE_UA} kiroCrewRelayWindow kiroCrewRelayPane kiroCrewRelayPaneHeaderless`],
  ])('draws no header for %s, exactly as on main', async (_label, ua) => {
    vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(ua)
    const { store, queryClient } = renderWithProviders(<App />, { route: '/chat' })
    expect(header()).toBeNull()
    expect(document.querySelector('[data-relay-pane-header]')).toBeNull()
    expect(screen.getByTestId('dashboard-shell').style.gridTemplateRows).toBe('0px minmax(0,1fr)')
    act(() => { store.dispatch(sseConnected()) })
    expect(screen.getByTestId('relay-pane-gateway-status')).toHaveTextContent(en.app.gateway_connected)
    await shellSettled(queryClient)
  })

  it.each([
    ['a relay Crew in its own window', `${BASE_UA} kiroCrewRelayWindow`],
    ['the header token without the pane token', `${BASE_UA} kiroCrewRelayWindow kiroCrewRelayPaneHeader`],
    ['a plain dashboard', 'Mozilla/5.0 (Macintosh)'],
  ])('keeps its whole header, switcher included, for %s', async (_label, ua) => {
    vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(ua)
    const { queryClient } = renderWithProviders(<App />, { route: '/chat' })
    expect(header()).not.toBeNull()
    expect(header()).not.toHaveAttribute('data-relay-pane-header')
    expect(screen.getByTestId('dashboard-shell').style.gridTemplateRows).toBe('')
    // Wait for CrewSwitcher's lazy TeamCrewsSwitcher (TeamCrewsGroup) boundary.
    expect(await screen.findByTestId('crew-tab-strip', undefined, SHELL_SETTLED)).toBeInTheDocument()
    await shellSettled(queryClient)
  })
})

/** A desktop shell's pane half, as the dashboard sees it. */
function installBridge() {
  const listeners = new Set<(e: RelayPaneEvent) => void>()
  const ok = vi.fn(async () => ({ ok: true }))
  ;(window as unknown as { relayCrewAPI?: unknown }).relayCrewAPI = {
    open: ok, closeAll: ok, openPane: ok, prewarmPane: ok, setPaneBounds: ok, setPaneOverlay: ok, setPaneInactive: ok, closePane: ok,
    paneLoadEvents: true,
    onPaneEvent: (cb: (e: RelayPaneEvent) => void) => { listeners.add(cb); return () => { listeners.delete(cb) } },
  }
  /** As electron/preload.js hands it over: every field present, `header` true only when main said so. */
  const emit = (type: string, endpointId: string, more: Partial<RelayPaneEvent> = {}) =>
    act(() => { for (const cb of [...listeners]) cb({ type, endpointId, index: -1, dark: null, header: false, ...more }) })
  return { emit, listeners }
}

const note = (over: Partial<Notification> = {}): Notification =>
  ({ kind: 'cron', ts: '2026-11-27T10:00:00.000Z', title: 'Local note', body: 'from the local Crew', acked: false, ...over }) as Notification

/** The desktop app's own page with relay tab `r1` in front and its pane host listening. */
async function localWithRelayTab(preloadedState?: Partial<RootState>) {
  const bridge = installBridge()
  const utils = renderWithProviders(<NavigationLeaveGuardProvider><App /></NavigationLeaveGuardProvider>, { route: '/chat', preloadedState })
  act(() => { utils.store.dispatch(sseConnected()) })
  act(() => { utils.store.dispatch(relayPaneOpened({ id: 'r1', name: 'Shared' })) })
  // Wait for lazy RelayCrewPaneHost to register the listener that reads `header`.
  await waitFor(() => expect(bridge.listeners.size).toBeGreaterThan(0), SHELL_SETTLED)
  // Wait for CrewSwitcher's lazy TeamCrewsSwitcher (TeamCrewsGroup) boundary.
  await screen.findByTestId('crew-tab-strip', undefined, SHELL_SETTLED)
  await shellSettled(utils.queryClient)
  return { ...utils, ...bridge }
}

const whole = () => {
  expect(bell()).not.toBeNull()
  expect(capsule()).not.toBeNull()
  expect(search()).not.toBeNull()
  expect(screen.getByTestId('focus-mode-toggle')).toBeInTheDocument()
  expect(header()).not.toHaveAttribute('data-relay-tab-strip-only')
  expect(screen.queryByTestId('tab-strip-gateway-status')).toBeNull()
}
const tabStripOnly = () => {
  expect(bell()).toBeNull()
  expect(capsule()).toBeNull()
  expect(search()).toBeNull()
  expect(screen.queryByTestId('focus-mode-toggle')).toBeNull()
  expect(header()).toHaveAttribute('data-relay-tab-strip-only')
  // The local gateway's state is still spoken, by words only a screen reader gets.
  expect(screen.getByTestId('tab-strip-gateway-status')).toHaveTextContent(en.app.gateway_connected)
  // What stays: the bar itself, the tab strip in it, and the bell's mounted remainder.
  expect(within(header() as HTMLElement).getByTestId('crew-tab-strip')).toBeInTheDocument()
  expect(within(header() as HTMLElement).getByTestId('notifications-bell-host')).toBeInTheDocument()
}

describe('the desktop app\'s own bar above a relay Crew tab', () => {
  it('asks the shell for a pane header on every open, which an older dashboard never does', async () => {
    const { openRelayPane, relayPaneBridge } = await import('../lib/relayCrewPanes')
    const { listeners } = installBridge()
    void listeners
    const bridge = relayPaneBridge()!
    const { store } = renderWithProviders(<div />)
    await openRelayPane(bridge, { id: 'r9', name: 'Nine' }, store.dispatch)
    const call = vi.mocked(bridge.openPane).mock.calls.at(-1)!
    expect(call[0]).toBe('r9')
    expect(call[3]).toBe(true)
  })

  it('stays whole until the shell confirms the pane\'s header, then keeps the tab strip only', async () => {
    const { emit } = await localWithRelayTab()
    // An older remote Crew never sends the event: the bar is main's bar.
    whole()
    // The page loading, or its theme, confirms nothing.
    emit('loaded', 'r1')
    emit('theme', 'r1', { dark: true })
    whole()
    emit('header', 'r1', { header: true })
    tabStripOnly()
  })

  it('takes its controls back when the tab changes, the page reloads without a header, or the pane closes', async () => {
    const { emit, store } = await localWithRelayTab()
    emit('header', 'r1', { header: true })
    tabStripOnly()
    // The local Crew comes forward: its own bar, whole.
    act(() => { store.dispatch(setActiveId(null)) })
    whole()
    // Back on the relay tab: still confirmed for that pane.
    act(() => { store.dispatch(relayPaneOpened({ id: 'r1', name: 'Shared' })) })
    tabStripOnly()
    // Another relay tab, whose page was never confirmed.
    act(() => { store.dispatch(relayPaneOpened({ id: 'r2', name: 'Older' })) })
    whole()
    act(() => { store.dispatch(relayPaneOpened({ id: 'r1', name: 'Shared' })) })
    tabStripOnly()
    // The pane's page navigated or reloaded: the shell says false until it is read again.
    emit('header', 'r1', { header: false })
    whole()
    emit('header', 'r1', { header: true })
    tabStripOnly()
    // An SSH Crew tab comes forward: its own iframe draws its header, and this bar is whole.
    act(() => { store.dispatch(setActiveId('ssh-1')) })
    expect(relayHeaderInFront(store.getState())).toBe(false)
    whole()
    act(() => { store.dispatch(relayPaneOpened({ id: 'r1', name: 'Shared' })) })
    tabStripOnly()
    emit('closed', 'r1')
    whole()
    expect(store.getState().instances.relayHeaders).toEqual([])
    // A pane opened again under the same ID starts unconfirmed.
    act(() => { store.dispatch(relayPaneOpened({ id: 'r1', name: 'Shared' })) })
    whole()
  })

  it('counts only the literal true, for the pane it names', async () => {
    const { emit, store } = await localWithRelayTab()
    for (const untrusted of ['true', 1, {}, 'header', null, undefined]) {
      emit('header', 'r1', { header: untrusted as unknown as boolean })
      whole()
    }
    // A header confirmed for a pane that is not in front changes nothing here.
    emit('header', 'r2', { header: true })
    whole()
    // No other event type carries it.
    emit('theme', 'r1', { dark: true, header: true })
    emit('loaded', 'r1', { header: true })
    whole()
    expect(relayHeaderInFront(store.getState())).toBe(false)
    emit('header', 'r1', { header: true })
    expect(relayHeaderInFront(store.getState())).toBe(true)
    // The shell says it again each time the tab comes to the front: the same list, untouched.
    const listed = store.getState().instances.relayHeaders
    emit('header', 'r1', { header: true })
    expect(store.getState().instances.relayHeaders).toBe(listed)
    // All it decided: this bar's own controls. Which tab is in front is untouched.
    expect(store.getState().instances.relayActive).toBe('r1')
    expect(store.getState().instances.activeId).toBeNull()
  })

  it('in focus mode the bar that slides in is the same tab strip, and leaving the relay tab brings the toggle back', async () => {
    const { emit, store } = await localWithRelayTab()
    fireEvent.click(screen.getByTestId('focus-mode-toggle'))
    expect(screen.getByTestId('focus-mode-announce').textContent).not.toBe('')
    emit('header', 'r1', { header: true })
    tabStripOnly()
    // Still the one header, out of the grid as focus mode keeps it.
    expect((header() as HTMLElement).style.position).toBe('absolute')
    act(() => { store.dispatch(setActiveId(null)) })
    whole()
    expect(screen.getByTestId('focus-mode-toggle')).toHaveAttribute('aria-pressed', 'true')
    // Focus mode lives outside the store this test made: turn it off again, so the tests
    // after this one, in any order, start from the ordinary bar.
    fireEvent.click(screen.getByTestId('focus-mode-toggle'))
    expect(screen.getByTestId('focus-mode-toggle')).toHaveAttribute('aria-pressed', 'false')
  })

  it('keeps the update pill beside the tab strip, and a click on it brings the local Crew forward', async () => {
    const status = vi.mocked(api.status)
    const plain = { uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }
    status.mockResolvedValue({ ...plain, update_available: true } as Awaited<ReturnType<typeof api.status>>)
    try {
      const { emit, store } = await localWithRelayTab()
      // Wait for ShellHeader's lazy UpdatePill boundary after status reports an update.
      expect(await screen.findByTestId('update-pill', undefined, SHELL_SETTLED)).toBeInTheDocument()
      emit('header', 'r1', { header: true })
      tabStripOnly()
      const pill = within(header() as HTMLElement).getByTestId('update-pill')
      // Settings is the local Crew's page, under the pane: the pill brings that Crew
      // forward, as a rail row does, so the click is not lost behind the relay Crew.
      fireEvent.click(pill)
      expect(store.getState().instances.relayActive).toBeNull()
      whole()
    } finally {
      status.mockResolvedValue(plain as Awaited<ReturnType<typeof api.status>>)
    }
  })

  it('hides the bell\'s button only: the dock badge still counts local notes and a local banner still shows and opens', async () => {
    const setBadgeCount = vi.fn()
    ;(window as unknown as { electronAPI?: unknown }).electronAPI = { setBadgeCount }
    const { emit, store } = await localWithRelayTab()
    act(() => {
      store.dispatch(addNotification(note({ ts: '2026-11-27T10:00:01.000Z' })))
      store.dispatch(addNotification(note({ ts: '2026-11-27T10:00:02.000Z' })))
    })
    await waitFor(() => expect(setBadgeCount).toHaveBeenLastCalledWith(2))
    emit('header', 'r1', { header: true })
    tabStripOnly()
    expect(setBadgeCount).toHaveBeenLastCalledWith(2)
    // A local note arrives while the relay Crew is in front: counted on the dock, and
    // bannered, as on main.
    const arriving = note({ ts: '2026-11-27T10:00:03.000Z', title: 'Arrived' })
    act(() => {
      store.dispatch(addNotification(arriving))
      dispatchLiveNotification(arriving)
    })
    await waitFor(() => expect(setBadgeCount).toHaveBeenLastCalledWith(3))
    expect(await screen.findByTestId('notification-banner-card')).toBeInTheDocument()
    tabStripOnly()
    // Main's behaviour for a tap on it: the local Crew comes forward and its sheet opens
    // on that note, so the bell that owns the sheet is back with it.
    fireEvent.click(screen.getByRole('button', { name: 'Open notification: Arrived' }))
    expect(store.getState().instances.relayActive).toBeNull()
    whole()
    expect(bell()).toHaveAttribute('aria-expanded', 'true')
  })
})
