import { describe, it, expect, vi } from 'vitest'
import { render, screen, act, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import App, { calculateTopbarSearchLayout } from '../App'
import { sseConnected, sseDisconnected } from '../store/dashboardSlice'
import SegmentedControl from '../components/SegmentedControl'
import { safeSetItem } from '../utils/safeStorage'

// Mock all page components to isolate routing
vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => <div data-testid="system-page">SystemPage</div> }))
vi.mock('../pages/AgentsPage', () => ({ default: () => <div data-testid="agents-page">AgentsPage</div> }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => <div data-testid="projects-page">ProjectsPage</div> }))
vi.mock('../pages/LogsPage', () => ({ default: () => <div data-testid="logs-page">LogsPage</div> }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => <div data-testid="mc-agents-page">MCAgentsPage</div> }))
vi.mock('../pages/CapabilitiesPage', () => ({ default: () => <div data-testid="capabilities-page">CapabilitiesPage</div> }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => <div data-testid="notifications-page">NotificationsPage</div> }))
vi.mock('../pages/SchedulePage', () => ({ default: () => <div data-testid="schedule-page">SchedulePage</div> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }, { name: 'reviewer' }, { name: 'oracle' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { credits_used: 3044, credits_covered: 3044, credits_overage: 0, credits_plan: 10000, resets: '2026-07-01', plan: 'KIRO POWER', cost_usd: 0, overage_rate: '0.04' } }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
  },
  // Default to "no auth banner showing" so existing App tests render the
  // normal connected/offline pill paths. The dedicated auth-banner
  // suppression test lives in App.offlinePill.test.tsx.
  isAuthBannerShown: vi.fn(() => false),
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
}))

// Mock matchMedia for useTheme and useIsMobile (jsdom doesn't provide it)
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: query === '(prefers-color-scheme: dark)',
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })),
})

// ResizeObserver stub for jsdom (used by SegmentedControl)
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as typeof ResizeObserver

describe('App routing', () => {
  it('renders chat page at /chat', () => {
    renderWithProviders(<App />, { route: '/chat' })
    expect(screen.getByTestId('chat-page')).toBeInTheDocument()
  })

  it('redirects /agents to the Agent Capabilities panel', () => {
    renderWithProviders(<App />, { route: '/agents' })
    expect(screen.getByTestId('capabilities-page')).toBeInTheDocument()
  })

  it('renders projects page at /projects', () => {
    renderWithProviders(<App />, { route: '/projects' })
    expect(screen.getByTestId('projects-page')).toBeInTheDocument()
  })

  it('redirects /tasks to /projects', () => {
    renderWithProviders(<App />, { route: '/tasks' })
    expect(screen.getByTestId('projects-page')).toBeInTheDocument()
  })

  it('renders logs page at /logs', () => {
    renderWithProviders(<App />, { route: '/logs' })
    expect(screen.getByTestId('logs-page')).toBeInTheDocument()
  })

  it('redirects unknown routes to /chat', () => {
    renderWithProviders(<App />, { route: '/nonexistent' })
    expect(screen.getByTestId('chat-page')).toBeInTheDocument()
  })

  it('renders nav items', () => {
    renderWithProviders(<App />, { route: '/chat' })
    expect(screen.getByText('Sessions')).toBeInTheDocument()
    expect(screen.getByText('Agent Capabilities')).toBeInTheDocument()
    expect(screen.getByText('Settings')).toBeInTheDocument()
    // The App Store now rides the Apps section header as an accent link.
    expect(screen.getByText('Explore')).toBeInTheDocument()
    // The bottom-pinned contact row with its three external links.
    expect(screen.getByText('Contact Us')).toBeInTheDocument()
    expect(screen.getByLabelText('Kiro website (kiro.dev)')).toBeInTheDocument()
    expect(screen.getByLabelText('KiroCrew GitHub repository')).toBeInTheDocument()
    expect(screen.getByLabelText('Kiro Discord community')).toBeInTheDocument()
  })

  it('renders the registry-derived Artifacts and Knowledge nav items', () => {
    // Regression guard for the aaf7cfe stale-branch merge, which reverted the
    // registry-driven rail (`NAV_ITEMS = getBuiltinSurfaces().map(...)`) back
    // to a hardcoded array that omitted Artifacts and Knowledge. Both are
    // registered unconditionally in `surfaces/builtins.tsx`, so they must
    // always appear in the rail. Asserting them by label catches a future
    // hardcoded-array regression that the isolated surfaces.test.tsx cannot.
    renderWithProviders(<App />, { route: '/chat' })
    expect(screen.getByText('Artifacts')).toBeInTheDocument()
    expect(screen.getByText('Knowledge')).toBeInTheDocument()
  })

  it('does not double-render Secretary when the builtin Secretary app is enabled', async () => {
    // Regression for the Surface registry refactor: Secretary registers a
    // surface (so its attention badge wires through `selectSurfaceBadgeCount`)
    // but is rendered as a nav item by `appNavItems` from `api.listApps()`,
    // not by NAV_ITEMS. With `appOnly: true` on the Secretary surface,
    // `getBuiltinSurfaces()` excludes it from NAV_ITEMS so it should appear
    // exactly once even when api.listApps() returns it.
    const { api } = await import('../api/client')
    ;(api.listApps as ReturnType<typeof vi.fn>).mockResolvedValueOnce([
      {
        name: 'secretary',
        displayName: 'Secretary',
        enabled: true,
        origin: 'builtin',
        manifest: { ui: { pages: [{ route: '/secretary', icon: 'Inbox', label: 'Secretary' }] } },
      },
    ])
    renderWithProviders(<App />, { route: '/chat' })
    // Wait for refreshAppNav() to complete and merge into the rail.
    await screen.findByText('Secretary')
    // Exactly one nav entry — never two. The duplicate-key React warning
    // would silently fire if both NAV_ITEMS and appNavItems contributed an
    // entry; this assertion catches the visible regression.
    expect(screen.getAllByText('Secretary')).toHaveLength(1)
  })

  it('collapses a long Apps list behind a "more" toggle so the nav cannot grow unbounded', async () => {
    // Regression for the nav-overflow bug: with many enabled apps the rail used
    // to grow past the viewport. The Apps group now shows up to APPS_NAV_LIMIT
    // (6) and hides the rest behind a "show more" toggle.
    const { api } = await import('../api/client')
    const manyApps = Array.from({ length: 10 }, (_, i) => ({
      name: `app${i}`,
      displayName: `App ${i}`,
      enabled: true,
      origin: 'installed',
      manifest: { ui: { pages: [{ route: `/apps/app${i}`, icon: 'Package', label: `App ${i}` }] } },
    }))
    ;(api.listApps as ReturnType<typeof vi.fn>).mockResolvedValueOnce(manyApps)
    localStorage.setItem('mc-apps-expanded', '0')
    renderWithProviders(<App />, { route: '/chat' })
    // The "more" toggle appears once the list overflows.
    const moreToggle = await screen.findByTitle(/more app/i)
    expect(moreToggle).toBeInTheDocument()
    // Some later app is hidden while collapsed...
    expect(screen.queryByText('App 9')).not.toBeInTheDocument()
    // ...and revealed after expanding.
    act(() => { moreToggle.click() })
    expect(await screen.findByText('App 9')).toBeInTheDocument()
    // Toggle now offers to collapse again.
    expect(screen.getByTitle(/show fewer apps/i)).toBeInTheDocument()
  })

  it('keeps the overflow toggle visible while expanded (no disappear / layout shift)', async () => {
    // Regression for the toggle-disappears bug: the toggle must render whenever
    // the Apps list is collapsible (length > APPS_NAV_LIMIT), not only when
    // hiddenCount > 0 — otherwise it vanishes (e.g. when the active app is the
    // sole overflow item, pulled into the visible set), causing a layout shift.
    const { api } = await import('../api/client')
    const apps = Array.from({ length: 8 }, (_, i) => ({
      name: `ovf${i}`,
      displayName: `Ovf ${i}`,
      enabled: true,
      origin: 'installed',
      manifest: { ui: { pages: [{ route: `/apps/ovf${i}`, icon: 'Package', label: `Ovf ${i}` }] } },
    }))
    ;(api.listApps as ReturnType<typeof vi.fn>).mockResolvedValueOnce(apps)
    // Expanded: hiddenCount is 0 but the list is still collapsible — the toggle
    // must remain (reading "Show less"), proving it doesn't hinge on hiddenCount.
    localStorage.setItem('mc-apps-expanded', '1')
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByTitle(/show fewer apps/i)).toBeInTheDocument()
  })

  it('refetches the Apps nav when the gateway reconnects (post-update recovery)', async () => {
    // Regression for the empty-rail-after-update bug: the dashboard fetches
    // /api/apps once on mount, and right after a `kirocrew update` restart that
    // first fetch can come back empty while the gateway is still warming. When
    // the WebSocket reconnects, the Apps nav must refetch and self-heal —
    // previously it stayed empty until a manual reload (Browse, lazy-fetched,
    // kept working, which is why apps still showed in the App Store).
    const { api } = await import('../api/client')
    const lateApp = {
      name: 'late', displayName: 'Late App', enabled: true, origin: 'installed',
      manifest: { ui: { pages: [{ route: '/apps/late', icon: 'Package', label: 'Late App' }] } },
    }
    ;(api.listApps as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce([])        // mount: gateway not ready, empty list
      .mockResolvedValueOnce([lateApp]) // after reconnect: app is now listed
    const store = createTestStore()
    renderWithProviders(<App />, { route: '/chat', store })
    // Let the (empty) mount fetch settle; the app is absent.
    await waitFor(() => expect(screen.getByText('Sessions')).toBeInTheDocument())
    expect(screen.queryByText('Late App')).not.toBeInTheDocument()
    // Simulate a `kirocrew update` restart: the WS connects, drops, reconnects.
    // Only the reconnect (after a drop) refetches the Apps nav — the rail
    // self-heals without a manual reload.
    act(() => { store.dispatch(sseConnected()) })
    act(() => { store.dispatch(sseDisconnected()) })
    act(() => { store.dispatch(sseConnected()) })
    expect(await screen.findByText('Late App')).toBeInTheDocument()
  })

  it('retries the initial Apps-nav fetch after a transient failure', async () => {
    // The mount fetch can reject while the gateway is mid-restart; the failure
    // used to be swallowed (empty rail). refreshAppNav now retries with bounded
    // backoff so the apps appear without a manual reload.
    vi.useFakeTimers()
    try {
      const { api } = await import('../api/client')
      const retryApp = {
        name: 'retryapp', displayName: 'Retry App', enabled: true, origin: 'installed',
        manifest: { ui: { pages: [{ route: '/apps/retryapp', icon: 'Package', label: 'Retry App' }] } },
      }
      ;(api.listApps as ReturnType<typeof vi.fn>)
        .mockRejectedValueOnce(new Error('gateway cold start'))
        .mockResolvedValueOnce([retryApp])
      renderWithProviders(<App />, { route: '/chat' })
      // Flush the rejected mount fetch, then advance past the first backoff
      // (500ms base) so the retry fires and resolves with the app.
      await act(async () => { await vi.advanceTimersByTimeAsync(600) })
      expect(screen.getByText('Retry App')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('cancels a pending retry when refreshAppNav is re-triggered (no overlapping chains)', async () => {
    // Regression for the overlapping-retry-chains race: if a trigger
    // (mc:apps-changed / reconnect) fires while a backoff retry from a failed
    // mount fetch is still pending, the pending retry must be cancelled so only
    // one fetch chain runs — otherwise the orphaned retry fires a stale fetch
    // that can overwrite the freshly-loaded nav with an empty list.
    vi.useFakeTimers()
    try {
      const { api } = await import('../api/client')
      const listApps = api.listApps as ReturnType<typeof vi.fn>
      const evApp = {
        name: 'evapp', displayName: 'Event App', enabled: true, origin: 'installed',
        manifest: { ui: { pages: [{ route: '/apps/evapp', icon: 'Package', label: 'Event App' }] } },
      }
      listApps.mockReset()
      listApps.mockResolvedValue([])                 // default for any stray call
      listApps.mockRejectedValueOnce(new Error('cold start')) // mount fetch fails → schedules retry
      listApps.mockResolvedValueOnce([evApp])        // the re-trigger resolves with the app
      renderWithProviders(<App />, { route: '/chat' })
      // Before the 500ms retry fires, re-trigger refreshAppNav.
      await act(async () => { await vi.advanceTimersByTimeAsync(100) })
      act(() => { window.dispatchEvent(new Event('mc:apps-changed')) })
      // Advance well past the original retry's deadline; it must NOT fire.
      await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
      expect(screen.getByText('Event App')).toBeInTheDocument()
      // Exactly two fetches: the failed mount + the re-trigger. The orphaned
      // retry was cancelled, so no third (empty) fetch overwrote the nav.
      expect(listApps).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows a portaled hover label for a collapsed (icon-only) nav item', async () => {
    // Covers useNavTip: in collapsed mode nav rows hide their text label and
    // instead show it via a portal to <body> on hover (so the rail's vertical
    // scroll-clip can't chop it). Hover -> the label text appears.
    const { fireEvent } = await import('@testing-library/react')
    localStorage.setItem('mc-nav', '1') // start sidebar collapsed
    const { container } = renderWithProviders(<App />, { route: '/chat' })
    // Collapsed nav items have no visible text; find a row by its class.
    const rows = await waitFor(() => {
      const found = container.querySelectorAll('nav [class*="group/nav"]')
      if (found.length === 0) throw new Error('no nav rows yet')
      return found
    })
    // The icon-only row still names itself for assistive tech via aria-label,
    // since the visible text only mounts on hover (no permanent DOM text node).
    expect(screen.getByLabelText('Sessions')).toBeInTheDocument()
    // Hover the first row -> its portaled label text should mount.
    fireEvent.mouseEnter(rows[0])
    expect(await screen.findByText('Sessions')).toBeInTheDocument()
    // Leave -> label begins fade-out (still present until the timer).
    fireEvent.mouseLeave(rows[0])
  })

  it('surfaces the collapsed hover label on keyboard focus and is Enter-activatable', async () => {
    // Keyboard-only users (no pointer) must still be able to identify icon-only
    // rows: the label appears on focus, not just mouseenter. The row is also a
    // real control (role=button + tabIndex) operable with Enter.
    const { fireEvent } = await import('@testing-library/react')
    localStorage.setItem('mc-nav', '1') // start sidebar collapsed
    const { container } = renderWithProviders(<App />, { route: '/chat' })
    const rows = await waitFor(() => {
      const found = container.querySelectorAll('nav [role="button"][class*="group/nav"]')
      if (found.length === 0) throw new Error('no focusable nav rows yet')
      return found
    })
    // Focusable as a button.
    expect(rows[0].getAttribute('tabindex')).toBe('0')
    // Focus -> the portaled label mounts (parity with hover).
    fireEvent.focus(rows[0])
    expect(await screen.findByText('Sessions')).toBeInTheDocument()
    // Blur -> begins fade-out (still mounted until the unmount timer).
    fireEvent.blur(rows[0])
    // Enter activates without throwing (navigates to the row's route).
    fireEvent.keyDown(rows[0], { key: 'Enter' })
  })

  it('renders Kiro Crew branding', () => {
    renderWithProviders(<App />, { route: '/chat' })
    expect(screen.getAllByText('Kiro Crew').length).toBeGreaterThan(0)
  })

  it('opens Search Everywhere from the theme-aware shadowless header trigger', () => {
    renderWithProviders(<App />, { route: '/chat' })
    const trigger = screen.getByRole('button', { name: 'Search sessions, files, and commands' })
    expect(trigger).toHaveClass('rounded-lg', 'border-border', 'bg-card', 'shadow-none')
    expect(trigger).not.toHaveClass('rounded-full')
    fireEvent.click(trigger)
    expect(screen.getByRole('dialog', { name: 'Search everywhere' })).toBeInTheDocument()
  })

  it('reserves the larger topbar cluster before showing the centered search', () => {
    expect(calculateTopbarSearchLayout(330, 180, 1200)).toEqual({ gutter: 342, visible: true })
    expect(calculateTopbarSearchLayout(180, 505, 1570)).toEqual({ gutter: 517, visible: true })
    expect(calculateTopbarSearchLayout(330, 180, 900)).toEqual({ gutter: 342, visible: false })
  })

  it('resizes the sidebar and main body together with a quick shell transition', () => {
    localStorage.removeItem('mc-nav')
    renderWithProviders(<App />, { route: '/chat' })

    const shell = screen.getByTestId('dashboard-shell')
    expect(shell).toHaveStyle({
      gridTemplateColumns: '236px minmax(0,1fr) auto',
      transition: 'none',
    })

    fireEvent.click(screen.getByRole('button', { name: 'Collapse sidebar' }))
    expect(shell).toHaveStyle({
      gridTemplateColumns: '74px minmax(0,1fr) auto',
      transition: 'grid-template-columns 150ms cubic-bezier(0.2, 0, 0, 1)',
    })
    localStorage.removeItem('mc-nav')
  })

  it('hosts the collapse control in the nav menu row and hides the Main group heading', () => {
    localStorage.removeItem('mc-nav')
    renderWithProviders(<App />, { route: '/chat' })

    const logo = screen.getByAltText('Kiro Crew')
    expect(logo).toHaveClass('w-9', 'h-9')

    const nav = screen.getByRole('navigation', { name: 'Main navigation' })
    // The sidebar toggle moved from the topbar into the rail's menu row:
    // a hamburger plus (expanded only) a panel-left-close collapse control.
    const collapse = within(nav).getByRole('button', { name: 'Collapse sidebar' })
    expect(within(nav).getByRole('button', { name: 'Toggle sidebar' })).toBeInTheDocument()
    expect(within(nav).queryByText('Main')).not.toBeInTheDocument()

    fireEvent.click(collapse)
    expect(within(nav).getByRole('button', { name: 'Expand sidebar' })).toBeInTheDocument()
    // Collapsed: the panel-left-close control unmounts, only the hamburger stays.
    expect(within(nav).queryByRole('button', { name: 'Collapse sidebar' })).not.toBeInTheDocument()
    expect(localStorage.getItem('mc-nav')).toBe('1')
    localStorage.removeItem('mc-nav')
  })

  it('hides the Contact Us row when the sidebar is collapsed', () => {
    localStorage.removeItem('mc-nav')
    renderWithProviders(<App />, { route: '/chat' })
    const nav = screen.getByRole('navigation', { name: 'Main navigation' })
    const contact = within(nav).getByText('Contact Us')
    expect(contact).toBeVisible()
    fireEvent.click(within(nav).getByRole('button', { name: 'Collapse sidebar' }))
    // The row folds away (max-h-0 + opacity-0 + inert) instead of unmounting.
    const wrapper = contact.closest('[class*="max-h-0"]')
    expect(wrapper).not.toBeNull()
    expect(wrapper).toHaveAttribute('inert')
    localStorage.removeItem('mc-nav')
  })

  it('keeps Request a Feature visible beside the collapsed brand icon', () => {
    safeSetItem('mc-nav', '1')
    renderWithProviders(<App />, { route: '/chat' })

    // Collapsed: brand shrinks to the icon but Request a Feature stays.
    expect(screen.getByRole('button', { name: 'Request a Feature' })).toBeInTheDocument()

    const nav = screen.getByRole('navigation', { name: 'Main navigation' })
    fireEvent.click(within(nav).getByRole('button', { name: 'Expand sidebar' }))
    expect(within(nav).getByRole('button', { name: 'Collapse sidebar' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Request a Feature' })).toBeInTheDocument()
    expect(localStorage.getItem('mc-nav')).toBe('0')
    localStorage.removeItem('mc-nav')
  })

  it('renders connection status', () => {
    renderWithProviders(<App />, { route: '/chat' })
    // Connection is a colored dot in the unified readout capsule ("Offline"
    // text was removed -- the capsule's red tint is the disconnected signal).
    expect(screen.getByLabelText('Gateway offline')).toBeInTheDocument()
  })

  it('renders theme toggle', () => {
    renderWithProviders(<App />, { route: '/chat' })
    // Default preference is 'system', button shows "Auto"
    expect(screen.getAllByText(/Auto|Light|Dark/).length).toBeGreaterThan(0)
  })

  it('renders approval mode buttons with tooltips', () => {
    // Mock clientWidth so SegmentedControl renders in full mode (not dropdown)
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, value: 500 })
    const segments = [
      { key: 'normal' as const, label: 'Normal', tooltip: 'Prompt for approval' },
      { key: 'trust' as const, label: 'Trust', tooltip: 'Auto-approve all tools' },
    ]
    const { container } = render(
      <SegmentedControl segments={segments} value="normal" onChange={() => {}} />
    )
    const buttons = container.querySelectorAll('button')
    expect(buttons).toHaveLength(2)
    expect(buttons[0]).toHaveAttribute('title', 'Prompt for approval')
    expect(buttons[1]).toHaveAttribute('title', 'Auto-approve all tools')
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, value: 0 })
  })
})

describe('TopbarMetrics widget', () => {
  it('shows only the Activity toggle button when metricsOpen is not set', () => {
    localStorage.removeItem('mc-topbar-metrics')
    renderWithProviders(<App />, { route: '/chat' })
    expect(screen.getByTitle('System metrics')).toBeInTheDocument()
    expect(screen.queryByText(/CPU /)).not.toBeInTheDocument()
    expect(screen.queryByText(/MEM /)).not.toBeInTheDocument()
  })

  it('persists toggle open state in localStorage and renders the metrics pill', async () => {
    localStorage.setItem('mc-topbar-metrics', '1')
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByText(/CPU 25%/)).toBeInTheDocument()
    expect(screen.getByText(/MEM 25%/)).toBeInTheDocument()
    expect(screen.getByText(/DSK 40%/)).toBeInTheDocument()
    localStorage.removeItem('mc-topbar-metrics')
  })

  it('renders placeholder dashes instead of NaN when memTotal or diskTotal is 0', async () => {
    const { api } = await import('../api/client')
    const sysMock = vi.mocked(api.system)
    sysMock.mockResolvedValueOnce({ mem_used_gb: 4.0, mem_total_gb: 0, cpu_pct: 25.0, disk_total_gb: 0, disk_free_gb: 0 } as never)
    localStorage.setItem('mc-topbar-metrics', '1')
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByText(/MEM —/)).toBeInTheDocument()
    expect(screen.getByText(/DSK —/)).toBeInTheDocument()
    sysMock.mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 } as never)
    localStorage.removeItem('mc-topbar-metrics')
  })

  it('renders "CPU —" instead of crashing when cpu_pct is undefined', async () => {
    const { api } = await import('../api/client')
    const sysMock = vi.mocked(api.system)
    // Backend omits cpu_pct (partial/stale frame or older gateway) -> cpuPct is undefined.
    sysMock.mockResolvedValueOnce({ mem_used_gb: 4.0, mem_total_gb: 16.0, disk_total_gb: 100.0, disk_free_gb: 60.0 } as never)
    localStorage.setItem('mc-topbar-metrics', '1')
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByText(/CPU —/)).toBeInTheDocument()
    // mem/disk still render normally from the same frame.
    expect(screen.getByText(/MEM 25%/)).toBeInTheDocument()
    sysMock.mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 } as never)
    localStorage.removeItem('mc-topbar-metrics')
  })

  it('renders "metrics unavailable" pill when api.system rejects', async () => {
    const { api } = await import('../api/client')
    const sysMock = vi.mocked(api.system)
    sysMock.mockRejectedValueOnce(new Error('boom'))
    localStorage.setItem('mc-topbar-metrics', '1')
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByText(/metrics unavailable/)).toBeInTheDocument()
    sysMock.mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 } as never)
    localStorage.removeItem('mc-topbar-metrics')
  })
})

describe('onCycleAgent keyboard shortcut', () => {
  it('cycles to next agent when Alt+Shift+A is pressed', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    // Set up the real singleton store state that onCycleAgent reads via store.getState()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, agent: 'kirocrew' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'A', code: 'KeyA', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotAgent).toHaveBeenCalledWith('slot-1', 'reviewer')
  })

  it('does not call api.chatSlotAgent when no active slot', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotAgent as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'chat/setActiveSlot', payload: null })
    renderWithProviders(<App />, { route: '/chat' })
    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'A', code: 'KeyA', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotAgent).not.toHaveBeenCalled()
  })
})

describe('onCycleAgent edge cases', () => {
  it('does not cycle agent when installedAgents is empty', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    const useAgentsMod = await import('../hooks/useAgents')
    const useAgentsMock = vi.mocked(useAgentsMod).useAgents
    useAgentsMock.mockReturnValue({ agents: [], defaultAgent: '' })
    ;(api.chatSlotAgent as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'A', code: 'KeyA', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotAgent).not.toHaveBeenCalled()
    useAgentsMock.mockReturnValue({ agents: [{ name: 'kirocrew' }, { name: 'reviewer' }, { name: 'oracle' }], defaultAgent: 'kirocrew' })
  })
})

describe('onCyclePrevAgent edge cases', () => {
  it('does not cycle prev agent when no active slot', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotAgent as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'chat/setActiveSlot', payload: null })
    renderWithProviders(<App />, { route: '/chat' })
    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Z', code: 'KeyZ', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotAgent).not.toHaveBeenCalled()
  })

  it('does not cycle prev agent when installedAgents is empty', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    const useAgentsMod = await import('../hooks/useAgents')
    const useAgentsMock = vi.mocked(useAgentsMod).useAgents
    useAgentsMock.mockReturnValue({ agents: [], defaultAgent: '' })
    ;(api.chatSlotAgent as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Z', code: 'KeyZ', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotAgent).not.toHaveBeenCalled()
    useAgentsMock.mockReturnValue({ agents: [{ name: 'kirocrew' }, { name: 'reviewer' }, { name: 'oracle' }], defaultAgent: 'kirocrew' })
  })
})

describe('onCycleApprovalMode and onCyclePrevApprovalMode no-slot cases', () => {
  it('does not cycle approval mode when no active slot', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatMode as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'chat/setActiveSlot', payload: null })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'F', code: 'KeyF', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatMode).not.toHaveBeenCalled()
  })

  it('does not cycle prev approval mode when no active slot', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatMode as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'chat/setActiveSlot', payload: null })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'V', code: 'KeyV', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatMode).not.toHaveBeenCalled()
  })
})

describe('onCycleReasoningEffort no-slot cases', () => {
  it('does not cycle reasoning effort when no active slot', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotReasoningEffort as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'chat/setActiveSlot', payload: null })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'D', code: 'KeyD', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotReasoningEffort).not.toHaveBeenCalled()
  })

  it('does not cycle prev reasoning effort when no active slot', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotReasoningEffort as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'chat/setActiveSlot', payload: null })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'C', code: 'KeyC', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotReasoningEffort).not.toHaveBeenCalled()
  })
})

describe('onCycleApprovalMode and onCyclePrevAgent shortcuts', () => {
  it('cycles approval mode forward on Alt+Shift+F', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'F', code: 'KeyF', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatMode).toHaveBeenCalledWith('trust_reads', 'slot-1')
  })

  it('cycles agent backward on Alt+Shift+Z', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotAgent as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, agent: 'reviewer' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Z', code: 'KeyZ', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotAgent).toHaveBeenCalledWith('slot-1', 'kirocrew')
  })

  it('cycles approval mode backward on Alt+Shift+V', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatMode as ReturnType<typeof vi.fn>).mockClear()
    // Force approvalMode to 'yolo' via fulfilled thunk action
    store.dispatch({ type: 'dashboard/changeApprovalMode/fulfilled', payload: 'yolo' })
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'V', code: 'KeyV', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatMode).toHaveBeenCalledWith('trust', 'slot-1')
  })

  it('cycles reasoning effort forward on Alt+Shift+D', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotReasoningEffort as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, reasoning_effort: '' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'D', code: 'KeyD', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotReasoningEffort).toHaveBeenCalledWith('slot-1', 'low')
  })

  it('cycles reasoning effort backward on Alt+Shift+C', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotReasoningEffort as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, reasoning_effort: 'low' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'C', code: 'KeyC', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotReasoningEffort).toHaveBeenCalledWith('slot-1', '')
  })
})

describe('Alt+Shift+S/X model cycling via React Query cache', () => {
  it('does not call chatSlotModel on Alt+Shift+S without cache', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotModel as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, model: 'claude-3' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'S', code: 'KeyS', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotModel).not.toHaveBeenCalled()
  })

  it('does not call chatSlotModel on Alt+Shift+X without cache', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotModel as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, model: 'claude-3' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'X', code: 'KeyX', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotModel).not.toHaveBeenCalled()
  })

  it('cycles to next model on Alt+Shift+S', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotModel as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, model: 'auto' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    const { queryClient } = renderWithProviders(<App />, { route: '/chat' })
    queryClient.setQueryData(['available-models', 'acp'], [{ name: 'auto' }, { name: 'opus' }, { name: 'sonnet' }])
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'S', code: 'KeyS', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotModel).toHaveBeenCalledWith('slot-1', 'opus')
  })

  it('cycles to previous model on Alt+Shift+X', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotModel as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, model: 'opus' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    const { queryClient } = renderWithProviders(<App />, { route: '/chat' })
    queryClient.setQueryData(['available-models', 'acp'], [{ name: 'auto' }, { name: 'opus' }, { name: 'sonnet' }])
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'X', code: 'KeyX', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotModel).toHaveBeenCalledWith('slot-1', 'auto')
  })
})

describe('Kiro credits pill', () => {
  it('shows a checking/loading state until usage resolves with plan data', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.sessionsUsage).mockResolvedValueOnce({ usage: {} } as never)
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByTitle(/Kiro credit usage/)).toBeInTheDocument()
  })

  it('renders used/limit and percentage once loaded', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    // default mock: 3044 total used of 10000 = 30%
    const pill = await screen.findByTitle(/Kiro credits: 3,044 \/ 10,000 \(30%\)/)
    expect(pill).toBeInTheDocument()
  })

  it('renders the true total (credits_used) including overage above the plan', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.sessionsUsage).mockResolvedValueOnce({
      usage: { credits_covered: 10000, credits_used: 10500, credits_overage: 500, credits_plan: 10000, resets: '2026-07-01', plan: 'KIRO POWER' },
    } as never)
    renderWithProviders(<App />, { route: '/chat' })
    // credits_used=10500 total / 10000 plan = 105% (500 over plan)
    expect(await screen.findByTitle(/Kiro credits: 10,500 \/ 10,000 \(105%\)/)).toBeInTheDocument()
  })

  it('opens a details modal with breakdown rows when clicked', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    const pill = await screen.findByTitle(/Kiro credits: 3,044/)
    fireEvent.click(pill)
    expect(await screen.findByText('KIRO POWER')).toBeInTheDocument()
    expect(screen.getByText('2026-07-01')).toBeInTheDocument()
    expect(screen.getByText('Overage used')).toBeInTheDocument()
    expect(screen.getByText(/across chat, agents, MCP/)).toBeInTheDocument()
  })
})

describe('Kiro credits pill — edge cases', () => {
  it('stays in loading state if the usage fetch rejects', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.sessionsUsage).mockRejectedValueOnce(new Error('boom'))
    renderWithProviders(<App />, { route: '/chat' })
    // useQuery (retry:false) surfaces the error and leaves data undefined; pill stays in the checking/loading state
    expect(await screen.findByTitle(/Kiro credit usage/)).toBeInTheDocument()
  })

  it('opens the modal in a loading state when clicked before data resolves', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.sessionsUsage).mockResolvedValueOnce({ usage: {} } as never)
    renderWithProviders(<App />, { route: '/chat' })
    const loadingPill = await screen.findByTitle(/Kiro credit usage/)
    fireEvent.click(loadingPill)
    const loadingMsg = await screen.findByText(/Checking usage/)
    expect(loadingMsg).toBeInTheDocument()
    // The whole message is wrapped in one <span> so the flex row renders it as
    // flowing prose instead of fragmenting each text run into its own column.
    expect(loadingMsg.tagName).toBe('SPAN')
    expect(loadingMsg.querySelector('code')?.textContent).toBe('kiro-cli /usage')
  })

  it('defaults covered/overage to 0 and renders sub-1000 values without K suffix', async () => {
    const { api } = await import('../api/client')
    // only credits_plan present -> credits_used falls back to 0
    vi.mocked(api.sessionsUsage).mockResolvedValueOnce({ usage: { credits_plan: 500 } } as never)
    renderWithProviders(<App />, { route: '/chat' })
    const pill = await screen.findByTitle(/Kiro credits: 0 \/ 500 \(0%\)/)
    expect(pill).toHaveTextContent('0/500') // sub-1000 -> no "K" formatting
    fireEvent.click(pill)
    expect(await screen.findByText('0 credits')).toBeInTheDocument() // Overage used row
  })

  it('handles a zero limit without dividing by zero (0%)', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.sessionsUsage).mockResolvedValueOnce({ usage: { credits_plan: 0, credits_covered: 0 } } as never)
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByTitle(/Kiro credits: 0 \/ 0 \(0%\)/)).toBeInTheDocument()
  })

  it('falls back to an empty object when the response has no usage key', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.sessionsUsage).mockResolvedValueOnce({} as never)
    renderWithProviders(<App />, { route: '/chat' })
    // d?.usage is undefined -> `|| {}` -> credits_plan absent -> stays loading
    expect(await screen.findByTitle(/Kiro credit usage/)).toBeInTheDocument()
  })

  it('closes the modal on Escape', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    const pill = await screen.findByTitle(/Kiro credits: 3,044/)
    fireEvent.click(pill)
    expect(await screen.findByText('Overage used')).toBeInTheDocument()
    act(() => { fireEvent.keyDown(window, { key: 'Escape' }) })
    await waitFor(() => expect(screen.queryByText('Overage used')).not.toBeInTheDocument())
  })

  it('hides the pill entirely when usage is unavailable (non-Kiro provider)', async () => {
    const { api } = await import('../api/client')
    // Backend reports available:false when kiro-cli is absent (e.g. a Claude-only provider).
    vi.mocked(api.sessionsUsage).mockResolvedValue({ usage: { available: false } } as never)
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(screen.queryByTitle(/Kiro credit usage/)).not.toBeInTheDocument())
    expect(screen.queryByTitle(/Kiro credits:/)).not.toBeInTheDocument()
  })

  it('auto-closes the modal if usage resolves to unavailable while it is open', async () => {
    const { api } = await import('../api/client')
    let resolveUsage: (v: unknown) => void = () => {}
    vi.mocked(api.sessionsUsage).mockReturnValue(new Promise(r => { resolveUsage = r }) as never)
    renderWithProviders(<App />, { route: '/chat' })
    const pill = await screen.findByTitle(/Kiro credit usage/)
    fireEvent.click(pill)
    expect(await screen.findByText(/Checking usage/)).toBeInTheDocument()
    await act(async () => { resolveUsage({ usage: { available: false } }); await Promise.resolve() })
    await waitFor(() => expect(screen.queryByText(/Checking usage/)).not.toBeInTheDocument())
  })

  it('never renders NaN when credit fields arrive non-finite', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.sessionsUsage).mockResolvedValue({ usage: { credits_plan: NaN, credits_used: NaN, credits_covered: NaN } } as never)
    renderWithProviders(<App />, { route: '/chat' })
    // Non-finite plan is rejected by the Number.isFinite guard, so the loaded
    // pill (which would otherwise show "NaN / NaN") never appears.
    await waitFor(() => expect(screen.queryByTitle(/Kiro credits:/)).not.toBeInTheDocument())
    expect(screen.queryByText(/NaN/)).not.toBeInTheDocument()
  })
})
