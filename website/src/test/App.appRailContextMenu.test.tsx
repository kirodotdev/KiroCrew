/**
 * The sidebar Apps list's right-click menu (#7883), pinned at the App level:
 * real shell, real rail, real drag sensors, mocked pages. The menu is composed
 * from the rail's app rows, the registry's update map and the rail's dnd-kit
 * sensors, so only the assembled shell shows how the pieces interact. The
 * contract this file pins:
 *
 * - a right-click on an installed app's row opens a menu at the pointer with
 *   Details and Unpin, and focus moves into it. When the registry reports a
 *   newer version, whatever the app's lifecycle, Details carries an update
 *   badge, and the badge's tooltip naming that version is also the item's
 *   accessible description. When the registry read fails, the menu shows the
 *   failure through `ErrorNotice`, with an Ask the agent item the notice
 *   describes.
 * - Details leaves for the detail page through the navigation leave guard, so
 *   a page holding a draft is asked first and a refusal keeps the user where
 *   they are. It carries no router state, so the detail page starts no update.
 * - Unpin writes the row's id into `mc-app-nav-hidden`, the row leaves the
 *   rail through its live filter, and focus lands on the next app row rather
 *   than on the detached one.
 * - a left click still navigates, and the active row pulled in from the "N
 *   more" overflow has the menu. A touch hold on a draggable row lifts it for
 *   reorder and opens no menu. Escape closes the menu without closing the
 *   phone nav drawer.
 *
 * Navigation checks read a location probe rendered beside `App`, so none of
 * them waits on a lazily loaded page. Same isolation shape as
 * App.appNavHiddenFilter.test.tsx: routed pages are mocked so App mounts
 * without real page trees.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, act, fireEvent, within } from '@testing-library/react'
import { useLocation } from 'react-router-dom'
import { renderWithProviders } from './helpers'
import App from '../App'
import { NavigationLeaveGuardProvider } from '../components/NavigationLeaveGuard'
import { api } from '../api/client'
import { APP_NAV_HIDDEN_KEY } from '../lib/appNavHidden'

/** The page on screen at /chat. It registers the leave guard the tests flip. */
const leaveGuard = vi.hoisted(() => ({ allow: true }))

vi.mock('../pages/ChatPage', async () => {
  const { useRegisterNavigationLeaveGuard } = await import('../components/NavigationLeaveGuard')
  function ChatPageStub() {
    useRegisterNavigationLeaveGuard(() => leaveGuard.allow)
    return null
  }
  return { default: ChatPageStub }
})
vi.mock('../pages/AppDetailPage', () => ({ default: () => null }))
vi.mock('../pages/apps/DiscoverPage', () => ({ default: () => null }))
vi.mock('../pages/apps/LibraryPage', () => ({ default: () => null }))
vi.mock('../pages/AppPage', () => ({ default: () => null }))
vi.mock('../pages/MigrationPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/SettingsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../pages/HooksPage', () => ({ default: () => null }))
vi.mock('../pages/CapabilitiesPage', () => ({ default: () => null }))
vi.mock('../pages/KnowledgePage', () => ({ default: () => null }))
vi.mock('../pages/DeveloperPage', () => ({ default: () => null }))
vi.mock('../pages/ArtifactsPage', () => ({ default: () => null }))
vi.mock('../pages/ArtifactDetailPage', () => ({ default: () => null }))
vi.mock('../pages/ArtifactDeployPage', () => ({ default: () => null }))
vi.mock('../pages/EmbedSettingsPage', () => ({ default: () => null }))
vi.mock('../pages/PopoutFrame', () => ({ default: () => null }))
vi.mock('../pages/ArtifactPopoutFrame', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {}, subscribeSubagents: () => {}, forceReconnect: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../hooks/useDashboardHealthProbe', () => ({ useDashboardHealthProbe: () => {} }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { available: false } }),
    listApps: vi.fn().mockResolvedValue([]),
    listRegistry: vi.fn().mockResolvedValue({ apps: [], categoryOrder: [], editorialSections: [] }),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
    approvals: vi.fn().mockResolvedValue([]),
  },
  isAuthBannerShown: vi.fn(() => false),
  ApiError: class extends Error { status: number; constructor(s: number, m: string) { super(m); this.status = s } },
}))

/**
 * Set the viewport `useIsMobile` reads. The hook caches on `window.matchMedia`'s
 * function identity, so a fresh function per test makes it re-read the
 * breakpoint.
 */
function setViewport(phone: boolean) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: phone && query === '(max-width: 767px)', media: query, onchange: null,
      addListener: vi.fn(), removeListener: vi.fn(),
      addEventListener: vi.fn(), removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  })
}
setViewport(false)
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver

/**
 * An installed app with a UI page, so `appNavTarget` gives it a sidebar row
 * (`app-<name>`). Path-installed and gateway-managed by default.
 */
function mkApp(name: string, displayName: string, extra: Record<string, unknown> = {}) {
  return {
    name,
    displayName,
    enabled: true,
    origin: 'local',
    lifecycle: 'gateway',
    source: `/home/dev/apps/${name}`,
    manifest: { ui: { entry: 'index.mjs', pages: [{ route: '/', label: displayName }] } },
    ...extra,
  }
}
const pathApp = mkApp('path-demo', 'Path Demo')
const keeperApp = mkApp('keeper-demo', 'Keeper Demo')
const registryApp = mkApp('reg-demo', 'Reg Demo', { origin: 'registry', source: 'registry:official' })
/** Manages its own updates (`app` lifecycle). The registry's update report still badges it. */
const selfManagedApp = mkApp('self-demo', 'Self Demo', { origin: 'registry', source: 'registry:official', lifecycle: 'app' })
/** A registry row reporting a newer version of the installed app. */
const registryUpdate = (name: string) => ({ name, displayName: name, version: '9.9.9', updateAvailable: true })
const registryNoUpdate = (name: string) => ({ name, displayName: name, version: '1.0.0', updateAvailable: false })

/** The router's location, rendered beside `App` instead of inside its routes. */
function LocationProbe() {
  const location = useLocation()
  const state = location.state as { autoAction?: string } | null
  return <div data-testid="location-probe" data-path={location.pathname} data-auto-action={state?.autoAction ?? ''} />
}

function renderApp({
  route = '/chat',
  apps = [pathApp, keeperApp],
  registry = [] as object[],
  registryFailure = null as Error | null,
} = {}) {
  vi.mocked(api.listApps).mockResolvedValue(apps)
  if (registryFailure) vi.mocked(api.listRegistry).mockRejectedValue(registryFailure)
  else vi.mocked(api.listRegistry).mockResolvedValue({ apps: registry, categoryOrder: [], editorialSections: [] })
  return renderWithProviders(
    <NavigationLeaveGuardProvider><App /><LocationProbe /></NavigationLeaveGuardProvider>,
    { route },
  )
}

const appRow = (name: string) => screen.getByRole('button', { name })
/** The rail shows the app rows once the apps fetch has landed. */
const railReady = (name: string) => waitFor(() => expect(screen.getByRole('button', { name })).toBeInTheDocument())
const openMenuOn = (row: HTMLElement) => {
  fireEvent.contextMenu(row, { clientX: 40, clientY: 20 })
  return screen.getByRole('menu')
}
const itemLabels = (menu: HTMLElement) => within(menu).getAllByRole('menuitem').map(item => item.textContent?.trim())
/**
 * Opens the menu on the named row, reads its labels, and closes it. A modal menu
 * hides the rail from the accessibility tree, so a retried read must close it
 * before the next attempt looks the row up.
 */
const menuLabelsOn = (name: string) => {
  const menu = openMenuOn(appRow(name))
  const labels = itemLabels(menu)
  fireEvent.keyDown(menu, { key: 'Escape' })
  return labels
}
const checkDetailsOn = (name: string, check: (details: HTMLElement) => void) => {
  const menu = openMenuOn(appRow(name))
  check(within(menu).getByRole('menuitem', { name: 'Details' }))
  fireEvent.keyDown(menu, { key: 'Escape' })
}
/**
 * Opens the named row's menu, waits for the element with `testId` in it, and
 * closes the menu. It waits with the menu open because a `waitFor` check that
 * opens and closes the menu mutates the DOM, each mutation re-runs the check
 * as a microtask, and a check that keeps failing then starves the timeout.
 */
const waitInMenuFor = async (name: string, testId: string) => {
  const menu = openMenuOn(appRow(name))
  await within(menu).findByTestId(testId)
  fireEvent.keyDown(menu, { key: 'Escape' })
}
const locationProbe = () => screen.getByTestId('location-probe')

describe('sidebar Apps rows: right-click menu', () => {
  beforeEach(() => {
    localStorage.clear()
    leaveGuard.allow = true
    setViewport(false)
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('opens Details and Unpin, in that order, on a right-click of an app row, with focus inside the menu', async () => {
    renderApp()
    await railReady('Path Demo')
    expect(screen.queryByRole('menu')).toBeNull()

    const menu = openMenuOn(appRow('Path Demo'))

    expect(itemLabels(menu)).toEqual(['Details', 'Unpin'])
    expect(within(menu).getByRole('menuitem', { name: 'Details' })).not.toHaveAccessibleDescription()
    expect(menu).toContainElement(document.activeElement as HTMLElement)
  })

  it('badges Details only when the registry reports a newer version, whatever the lifecycle, and adds no item', async () => {
    renderApp({
      apps: [registryApp, selfManagedApp, pathApp],
      registry: [registryUpdate('reg-demo'), registryUpdate('self-demo'), registryNoUpdate('path-demo')],
    })
    await railReady('Reg Demo')
    // The badges depend on the registry query, a separate read from the apps
    // query. Wait for one badge. Every row reads that same response.
    await waitInMenuFor('Reg Demo', 'app-rail-menu-update-badge')

    checkDetailsOn('Reg Demo', details => {
      expect(details).toHaveAccessibleDescription('Update to v9.9.9')
    })
    checkDetailsOn('Self Demo', details => {
      expect(details).toHaveAccessibleDescription('Update to v9.9.9')
      expect(within(details).getByTestId('app-rail-menu-update-badge')).toHaveAttribute('title', 'Update to v9.9.9')
    })
    checkDetailsOn('Path Demo', details => {
      expect(details).not.toHaveAccessibleDescription()
      expect(within(details).queryByTestId('app-rail-menu-update-badge')).toBeNull()
    })
    expect(menuLabelsOn('Reg Demo')).toEqual(['Details', 'Unpin'])
  })

  it('shows a failed registry read in the open menu, with an Ask the agent item the notice describes', async () => {
    renderApp({ registryFailure: new Error('registry unavailable') })
    await railReady('Path Demo')
    // The failure comes from the registry query, a separate read from the apps
    // query. Wait for its notice to reach the menu.
    await waitInMenuFor('Path Demo', 'app-rail-menu-registry-error')

    const menu = openMenuOn(appRow('Path Demo'))

    expect(within(menu).getByRole('alert')).toHaveTextContent('Failed to load registry')
    expect(within(menu).getByRole('menuitem', { name: 'Ask the agent' }))
      .toHaveAccessibleDescription('Failed to load registry')
    expect(itemLabels(menu)).toEqual(['Details', 'Unpin', 'Ask the agent'])
    expect(within(menu).queryByTestId('app-rail-menu-update-badge')).toBeNull()
  })

  it('Details opens the detail page without starting an update, and stays put when the page on screen refuses to be left', async () => {
    renderApp({ apps: [registryApp], registry: [registryUpdate('reg-demo')] })
    await railReady('Reg Demo')
    await waitInMenuFor('Reg Demo', 'app-rail-menu-update-badge')

    leaveGuard.allow = false
    openMenuOn(appRow('Reg Demo'))
    fireEvent.click(screen.getByTestId('app-rail-menu-details'))
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    expect(locationProbe()).toHaveAttribute('data-path', '/chat')

    leaveGuard.allow = true
    openMenuOn(appRow('Reg Demo'))
    fireEvent.click(screen.getByTestId('app-rail-menu-details'))
    await waitFor(() => expect(locationProbe()).toHaveAttribute('data-path', '/apps/detail/reg-demo'))
    // A badged Details carries no router state, so nothing starts the update
    // until the detail page's Update button is pressed.
    expect(locationProbe()).toHaveAttribute('data-auto-action', '')
  })

  it('Unpin hides the row through the stored hidden set and moves focus to the next app row', async () => {
    renderApp()
    await railReady('Path Demo')
    const row = appRow('Path Demo')
    act(() => { row.focus() })

    openMenuOn(row)
    fireEvent.click(screen.getByTestId('app-rail-menu-unpin'))

    await waitFor(() => expect(screen.queryByRole('button', { name: 'Path Demo' })).toBeNull())
    expect(JSON.parse(localStorage.getItem(APP_NAV_HIDDEN_KEY) || '[]')).toEqual(['app-path-demo'])
    expect(appRow('Keeper Demo')).toBeInTheDocument()
    // Radix would return focus to the row the menu opened on, and that row no
    // longer exists. The next row takes it instead.
    await waitFor(() => expect(document.activeElement).toBe(appRow('Keeper Demo')))
  })

  it('Unpin of the last app row hands focus to the Library row', async () => {
    renderApp({ apps: [pathApp] })
    await railReady('Path Demo')
    const row = appRow('Path Demo')
    act(() => { row.focus() })

    openMenuOn(row)
    fireEvent.click(screen.getByTestId('app-rail-menu-unpin'))

    await waitFor(() => expect(screen.queryByRole('button', { name: 'Path Demo' })).toBeNull())
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: /Library/ })))
  })

  it('a left click on an app row still navigates to the app', async () => {
    renderApp()
    await railReady('Path Demo')

    fireEvent.click(appRow('Path Demo'))

    await waitFor(() => expect(locationProbe()).toHaveAttribute('data-path', '/apps/path-demo'))
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('the active row pulled in from the "N more" overflow has the menu too', async () => {
    const names = ['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf']
    const apps = names.map(n => mkApp(n, n[0].toUpperCase() + n.slice(1)))
    // Seven apps with the route on the seventh. Six fit the limit, and the
    // active seventh is pulled in as a static, non-draggable row, so its
    // wrapper carries the menu trigger but no sortable touch-action.
    renderApp({ route: '/apps/golf', apps })
    await railReady('Golf')
    const wrapper = appRow('Golf').parentElement as HTMLElement
    expect(wrapper).toHaveAttribute('data-app-rail-row')
    expect(wrapper.style.touchAction).toBe('')

    const menu = openMenuOn(appRow('Golf'))

    expect(itemLabels(menu)).toEqual(['Details', 'Unpin'])
  })

  it('a touch hold on a draggable row lifts it for reorder and opens no menu', async () => {
    renderApp()
    await railReady('Path Demo')
    const row = appRow('Path Demo')
    vi.useFakeTimers()

    // One still finger fires both sequences. Radix arms its 700 ms hold on the
    // pointer events, and dnd-kit arms its 250 ms hold on the touch events.
    fireEvent.pointerDown(row, { pointerType: 'touch', clientX: 10, clientY: 10 })
    fireEvent.touchStart(row, { touches: [{ identifier: 1, clientX: 10, clientY: 10 }] })
    await act(async () => { await vi.advanceTimersByTimeAsync(250) })
    // The drag started. The overlay ghost is a second rendering of the row.
    expect(screen.getAllByRole('button', { name: 'Path Demo' })).toHaveLength(2)

    await act(async () => { await vi.advanceTimersByTimeAsync(450) })
    expect(screen.queryByRole('menu')).toBeNull()

    fireEvent.touchEnd(row, { touches: [], changedTouches: [{ identifier: 1, clientX: 10, clientY: 10 }] })
    fireEvent.pointerUp(row, { pointerType: 'touch', clientX: 10, clientY: 10 })
    // dnd-kit blocks clicks on the document for 50 ms after a drag ends. The
    // fake clock must pass that span, or the listener outlives this test and
    // swallows the next test's clicks.
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    expect(screen.getAllByRole('button', { name: 'Path Demo' })).toHaveLength(1)
  })
})

describe('sidebar Apps rows: right-click menu on a phone', () => {
  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem('mc-onboarded', '1')
    setViewport(true)
  })
  afterEach(() => {
    vi.useRealTimers()
    setViewport(false)
  })

  it('Escape closes the menu and leaves the nav drawer open', async () => {
    renderApp({ route: '/schedule' })
    // The header's Open menu button renders with the shell, not with the
    // lazily loaded page.
    await screen.findByRole('button', { name: 'Open menu' })
    // The drawer's slide and settle run on timers and animation frames. The
    // clock is faked before the drawer opens, so all of them run on it and
    // "still open" is read after the settle and not from a race with it.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'requestAnimationFrame', 'cancelAnimationFrame', 'performance'] })
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    const drawer = () => screen.queryByRole('navigation', { name: 'Main navigation' })
    expect(drawer()).toBeInTheDocument()
    const row = within(drawer() as HTMLElement).getByRole('button', { name: 'Path Demo' })

    const menu = openMenuOn(row)
    fireEvent.keyDown(menu, { key: 'Escape' })
    expect(screen.queryByRole('menu')).toBeNull()
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(drawer()).toBeInTheDocument()

    // Control. The same key with no menu open closes the drawer over the same span.
    fireEvent.keyDown(document.body, { key: 'Escape' })
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(drawer()).toBeNull()
  })
})
