import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import App, { calculateTopbarSearchLayout } from '../App'
import { sseConnected, sseDisconnected } from '../store/dashboardSlice'
import { openActivityPanel } from '../store/chatSlice'
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
    themes: vi.fn().mockResolvedValue({ themes: [] }),
    themeBoot: vi.fn().mockResolvedValue({
      mode: '',
      color: '',
      onboarded: true,
      import_onboarded: true,
    }),
    updateThemeConfig: vi.fn().mockResolvedValue({}),
    onboardingImportScan: vi.fn().mockResolvedValue({
      sources: [],
      skipped: [],
      merge_only: true,
    }),
    onboardingImportState: vi.fn().mockResolvedValue({}),
    // The first-run Privacy chapter renders the real TelemetryToggle.
    beaconStatus: vi.fn().mockResolvedValue({
      enabled: true,
      would_send: true,
      reason: 'ready',
      endpoint_configured: true,
      env_override: false,
      env_var: 'KIROCREW_TELEMETRY_DISABLED',
    }),
    patchConfig: vi.fn().mockResolvedValue({}),
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
  it('reopens the foreign-agent import gate when server onboarding is incomplete', async () => {
    const { api } = await import('../api/client')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-import-onboarded', '1')
    vi.mocked(api.themeBoot).mockResolvedValueOnce({
      mode: '',
      color: '',
      onboarded: false,
      import_onboarded: false,
    } as never)

    renderWithProviders(<App />, { route: '/chat' })

    await waitFor(() => expect(localStorage.getItem('mc-import-onboarded')).toBeNull())
    expect(await screen.findByRole('dialog', { name: 'Import agent setup' })).toBeInTheDocument()
  })

  it('migrates legacy browser-only onboarding before applying server defaults', async () => {
    const { api } = await import('../api/client')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.removeItem('mc-import-onboarded')
    vi.mocked(api.updateThemeConfig).mockClear()
    vi.mocked(api.themeBoot).mockResolvedValueOnce({
      mode: '',
      color: '',
      onboarded: false,
      import_onboarded: false,
    } as never)

    renderWithProviders(<App />, { route: '/chat' })

    await waitFor(() => {
      expect(api.updateThemeConfig).toHaveBeenCalledWith({
        onboarded: true,
        import_onboarded: true,
        // A finished legacy first run implies the disclosure is behind the user.
        // Persisted server-side so the gateway's first-heartbeat gate can see it.
        privacy_acked: true,
      })
      expect(localStorage.getItem('mc-import-onboarded')).toBe('1')
    })
    expect(screen.queryByRole('dialog', { name: 'Import agent setup' })).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: 'Welcome to Kiro Crew' })).not.toBeInTheDocument()
  })

  it('waits for theme boot before deciding the foreign-agent import gate', async () => {
    const { api } = await import('../api/client')
    let resolveBoot: (value: {
      mode: string
      color: string
      onboarded: boolean
      import_onboarded: boolean
    }) => void = () => {}
    vi.mocked(api.themeBoot).mockReturnValueOnce(new Promise(resolve => {
      resolveBoot = resolve
    }) as never)
    localStorage.removeItem('mc-onboarded')
    localStorage.removeItem('mc-import-onboarded')

    renderWithProviders(<App />, { route: '/chat' })

    expect(screen.queryByRole('dialog', { name: 'Import agent setup' })).not.toBeInTheDocument()
    await act(async () => {
      resolveBoot({ mode: '', color: '', onboarded: true, import_onboarded: true })
      await Promise.resolve()
    })
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: 'Import agent setup' })).not.toBeInTheDocument()
    })
  })

  // ── First-run chapter order: Import setup → Privacy → Customize ───────────
  // Privacy is MANDATORY, so these cover the two ways out of chapter 1: it
  // completing with nothing to import, and "Skip all".
  describe('first-run Privacy chapter', () => {
    const freshFirstRun = async () => {
      const { api } = await import('../api/client')
      localStorage.clear()
      vi.mocked(api.updateThemeConfig).mockClear()
      vi.mocked(api.themeBoot).mockResolvedValue({
        mode: '',
        color: '',
        onboarded: false,
        import_onboarded: false,
      } as never)
      return api
    }

    it('opens after Import setup and gates the Customize chapter', async () => {
      await freshFirstRun()
      // Nothing to import: the import chapter completes itself, and Privacy is
      // still shown rather than skipped along with it.
      renderWithProviders(<App />, { route: '/chat' })

      const dialog = await screen.findByRole('dialog', { name: 'Privacy' })
      expect(within(dialog).getByText('Anonymous daily heartbeat')).toBeInTheDocument()
      // Mandatory: no way past it but forward.
      expect(within(dialog).queryByRole('button', { name: /skip/i })).not.toBeInTheDocument()
      // The Customize chapter must not be reachable behind it.
      expect(screen.queryByText('Pick your look')).not.toBeInTheDocument()

      fireEvent.click(within(dialog).getByRole('button', { name: 'Continue' }))

      expect(await screen.findByText('Pick your look')).toBeInTheDocument()
      expect(localStorage.getItem('mc-privacy-acked')).toBe('1')
      // Persisted SERVER-side too, not just locally: the gateway withholds the
      // very first heartbeat until `dashboard.privacy_acked` is true, and it
      // cannot read localStorage. A local-only mark would leave the beacon
      // permanently silent on an install whose user did pass this chapter.
      const { api: clientApi } = await import('../api/client')
      await waitFor(() => {
        expect(clientApi.updateThemeConfig).toHaveBeenCalledWith({ privacy_acked: true })
      })
    })

    it('"Skip all" from the Customize chapter ends first run without re-showing Privacy', async () => {
      const api = await freshFirstRun()
      renderWithProviders(<App />, { route: '/chat' })

      // Chapter 1 (nothing to import) → Privacy → Customize.
      const dialog = await screen.findByRole('dialog', { name: 'Privacy' })
      fireEvent.click(within(dialog).getByRole('button', { name: 'Continue' }))
      expect(await screen.findByText('Pick your look')).toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: 'Skip all setup and onboarding' }))

      // Privacy is already behind the user, so the skip lands in the product.
      await waitFor(() =>
        expect(api.updateThemeConfig).toHaveBeenCalledWith({ onboarded: true }))
      expect(screen.queryByRole('dialog', { name: 'Privacy' })).not.toBeInTheDocument()
      expect(screen.queryByText('Pick your look')).not.toBeInTheDocument()
    })

    it('"Skip all" still lands on Privacy, then ends first run', async () => {
      const api = await freshFirstRun()
      vi.mocked(api.onboardingImportScan).mockResolvedValueOnce({
        sources: [{
          id: 'codex',
          name: 'Codex',
          detected: true,
          detail: '~/.codex',
          categories: [{
            id: 'instructions',
            label: 'Instructions',
            count: 2,
            description: 'Agent instructions',
          }],
        }],
        skipped: [],
        merge_only: true,
      } as never)

      renderWithProviders(<App />, { route: '/chat' })

      const importDialog = await screen.findByRole('dialog', { name: 'Import agent setup' })
      fireEvent.click(
        within(importDialog).getByRole('button', { name: 'Skip all setup and onboarding' }),
      )

      const dialog = await screen.findByRole('dialog', { name: 'Privacy' })
      // Skipping everything does not skip the disclosure — but nothing follows it.
      expect(api.updateThemeConfig).not.toHaveBeenCalledWith({ onboarded: true })

      fireEvent.click(within(dialog).getByRole('button', { name: 'Continue' }))

      await waitFor(() =>
        expect(api.updateThemeConfig).toHaveBeenCalledWith({ onboarded: true }))
      expect(screen.queryByText('Pick your look')).not.toBeInTheDocument()
    })

    // Escape IS "Skip all" — same routing, from a keystroke instead of the
    // header control. Asserted end-to-end because the two halves live apart:
    // the flow reports a skip, and App is what owes the user Privacy first.
    it('Escape in Import setup lands on Privacy, then ends first run', async () => {
      const api = await freshFirstRun()
      vi.mocked(api.onboardingImportScan).mockResolvedValueOnce({
        sources: [{
          id: 'codex',
          name: 'Codex',
          detected: true,
          detail: '~/.codex',
          categories: [{
            id: 'instructions',
            label: 'Instructions',
            count: 2,
            description: 'Agent instructions',
          }],
        }],
        skipped: [],
        merge_only: true,
      } as never)

      renderWithProviders(<App />, { route: '/chat' })
      await screen.findByRole('dialog', { name: 'Import agent setup' })

      fireEvent.keyDown(document, { key: 'Escape' })

      const dialog = await screen.findByRole('dialog', { name: 'Privacy' })
      expect(api.updateThemeConfig).not.toHaveBeenCalledWith({ onboarded: true })

      fireEvent.click(within(dialog).getByRole('button', { name: 'Continue' }))

      // Escape skipped the REST of first run, so Customize never opens.
      await waitFor(() =>
        expect(api.updateThemeConfig).toHaveBeenCalledWith({ onboarded: true }))
      expect(screen.queryByText('Pick your look')).not.toBeInTheDocument()
    })

    // A tree whose import chapter was completed by a build that PREDATES the
    // Privacy chapter: `mc-import-onboarded` is set, `mc-privacy-acked` is not.
    // The tour is seeded from localStorage BEFORE theme boot resolves, so this
    // holds boot pending — the window in which the derive effect cannot yet
    // correct anything, and the only thing standing between "Done" and the end
    // of first run is the guard on the completion path itself.
    it('cannot end first run from the pre-boot tour when Privacy is unacknowledged', async () => {
      const api = await freshFirstRun()
      localStorage.setItem('mc-import-onboarded', '1')
      // Boot never resolves for the duration of this test.
      vi.mocked(api.themeBoot).mockReturnValueOnce(new Promise(() => {}) as never)

      renderWithProviders(<App />, { route: '/chat' })

      // The seed must NOT put Customize on screen ahead of the disclosure.
      expect(screen.queryByText('Pick your look')).not.toBeInTheDocument()
      // And nothing may have marked first run complete.
      expect(api.updateThemeConfig).not.toHaveBeenCalledWith({ onboarded: true })
    })
  })

  it('renders chat page at /chat', () => {
    renderWithProviders(<App />, { route: '/chat' })
    expect(screen.getByTestId('chat-page')).toBeInTheDocument()
  })

  it('redirects /agents to the Agent Capabilities panel', () => {
    renderWithProviders(<App />, { route: '/agents' })
    expect(screen.getByTestId('capabilities-page')).toBeInTheDocument()
  })

  // /projects now resolves through BuiltinAppRoute -> BUILTIN_COMPONENT_REGISTRY
  // like every other builtin app page, so the component arrives lazily behind a
  // Suspense fallback. These two await it rather than querying synchronously.
  it('renders projects page at /projects', async () => {
    renderWithProviders(<App />, { route: '/projects' })
    expect(await screen.findByTestId('projects-page')).toBeInTheDocument()
  })

  it('redirects /tasks to /projects', async () => {
    renderWithProviders(<App />, { route: '/tasks' })
    expect(await screen.findByTestId('projects-page')).toBeInTheDocument()
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
    // The bottom-pinned community row: the GitHub mark fronts a "Star us" link
    // plus a "Report issue" BUTTON (it opens the diagnostics flow rather than
    // navigating to the issue list), and the icon-only Discord link. The
    // kiro.dev link was removed.
    expect(screen.getByText('Star us')).toBeInTheDocument()
    expect(screen.getByText('Report issue')).toBeInTheDocument()
    expect(screen.getByLabelText('Star Kiro Crew on GitHub')).toBeInTheDocument()
    expect(
      screen.getByLabelText(
        'Report a problem — collects logs and crash reports, secrets removed',
      ),
    ).toBeInTheDocument()
    // The old bare link to the issue list is gone — reporting now goes through
    // the collector so triage gets logs instead of an empty issue form.
    expect(screen.queryByLabelText('Report an issue on GitHub')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Kiro Discord community')).toBeInTheDocument()
    expect(screen.queryByLabelText('Kiro website (kiro.dev)')).not.toBeInTheDocument()
  })

  it('rail "Report issue" opens the diagnostics Report a Problem modal', async () => {
    // The rail entry used to be an <a> to /issues, which lost exactly what
    // triage needs. It must now mount the same shared modal as
    // Settings › About › Support.
    renderWithProviders(<App />, { route: '/chat' })
    const trigger = screen.getByLabelText(
      'Report a problem — collects logs and crash reports, secrets removed',
    )
    expect(trigger.tagName).toBe('BUTTON')
    expect(trigger).not.toHaveAttribute('href')

    fireEvent.click(trigger)
    expect(await screen.findByText('What happened?')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /create report/i })).toBeInTheDocument()
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

  it('dismisses the collapsed overflow-toggle hover label when the toggle is pressed', async () => {
    // Regression: pressing the Apps overflow toggle in the collapsed rail left
    // its portaled "N more" / "Show less" flyout on screen until the user
    // clicked elsewhere. Two causes: expanding re-flows the list so the row
    // moves out from under a stationary cursor (no mouseleave is dispatched),
    // and the click's own focus re-armed the label. Activation must dismiss it.
    const { fireEvent } = await import('@testing-library/react')
    const { api } = await import('../api/client')
    const manyApps = Array.from({ length: 10 }, (_, i) => ({
      name: `tipapp${i}`,
      displayName: `Tip App ${i}`,
      enabled: true,
      origin: 'installed',
      manifest: { ui: { pages: [{ route: `/apps/tipapp${i}`, icon: 'Package', label: `Tip App ${i}` }] } },
    }))
    ;(api.listApps as ReturnType<typeof vi.fn>).mockResolvedValueOnce(manyApps)
    localStorage.setItem('mc-nav', '1')          // collapsed (icon-only) rail
    localStorage.setItem('mc-apps-expanded', '0')
    renderWithProviders(<App />, { route: '/chat' })
    const toggle = await screen.findByTitle(/more app/i)
    // Hover -> the portaled label mounts (collapsed rows carry no inline text).
    fireEvent.mouseEnter(toggle)
    expect(await screen.findByText('4 more')).toBeInTheDocument()
    // Press it the way a mouse does: pointerdown -> focus -> click. Neither the
    // focus the press produces nor the surviving hover state may leave a label
    // on screen — and the dismissal must be immediate, with no fade-out: the
    // label text flips on activation, so a still-mounted fading label flashes
    // the OPPOSITE label ("Show less") as a ghost at the old coordinates.
    fireEvent.pointerDown(toggle)
    fireEvent.focus(toggle)
    act(() => { toggle.click() })
    expect(screen.queryByText('4 more')).toBeNull()
    expect(screen.queryByText('Show less')).toBeNull()
    // ...and the press still did its job: dismissing the label must not swallow
    // the toggle's own activation (the title flips once the list is expanded).
    expect(screen.getByTitle(/show fewer apps/i)).toBeInTheDocument()
    localStorage.removeItem('mc-nav')
    localStorage.removeItem('mc-apps-expanded')
  })

  it('renders Kiro Crew branding', () => {
    localStorage.removeItem('mc-nav') // expanded sidebar shows the brand text
    renderWithProviders(<App />, { route: '/chat' })
    // Brand (logo + name) moved from the top bar into the sidebar menu row.
    // The wordmark renders as two colored segments ('Kiro ' + 'Crew').
    expect(screen.getAllByText('Crew').length).toBeGreaterThan(0)
    localStorage.removeItem('mc-nav')
  })

  it('opens Search Everywhere from the theme-aware shadowless header trigger', () => {
    renderWithProviders(<App />, { route: '/chat' })
    const trigger = screen.getByRole('button', { name: 'Search sessions, files, and commands' })
    expect(trigger).toHaveClass('rounded-md', 'border-border', 'bg-card', 'shadow-none')
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
    // Regression (PR #94): the width transition was gated on a 180ms pulse AND
    // the Activity panel being closed, so the sidebar snapped instead of
    // animating whenever Activity was open (or a slow frame ate the pulse).
    // The transition must now be unconditional — including with Activity open.
    const store = createTestStore()
    store.dispatch(openActivityPanel())
    renderWithProviders(<App />, { route: '/chat', store })

    const shell = screen.getByTestId('dashboard-shell')
    expect(shell).toHaveStyle({
      gridTemplateColumns: '236px minmax(0,1fr) auto',
      transition: 'grid-template-columns 150ms cubic-bezier(0.2, 0, 0, 1)',
    })

    fireEvent.click(screen.getByRole('button', { name: 'Collapse sidebar' }))
    expect(shell).toHaveStyle({
      gridTemplateColumns: '74px minmax(0,1fr) auto',
      transition: 'grid-template-columns 150ms cubic-bezier(0.2, 0, 0, 1)',
    })
    localStorage.removeItem('mc-nav')
  })

  // ── Shell entrance animation is one-shot ──────────────────────────────────
  // The local pane is hidden (`display:none`), not unmounted, while a remote
  // instance tab is active. A CSS ANIMATION restarts when an element goes from
  // `display:none` back to displayed, so leaving `animate-rise` on the shell
  // replayed the whole dashboard's 350ms fade+lift on every return to the
  // Local tab. The class must retire itself after it has played once.
  it('retires the shell entrance animation once it has played', () => {
    renderWithProviders(<App />, { route: '/chat' })

    const shell = screen.getByTestId('dashboard-shell')
    expect(shell).toHaveClass('animate-rise')

    fireEvent.animationEnd(shell, { animationName: 'rise' })

    // Re-showing the pane cannot replay an animation that is no longer applied.
    expect(shell).not.toHaveClass('animate-rise')
  })

  it('does not retire the shell entrance from a descendant animation', () => {
    // `animationend` bubbles, and descendants (banners, cards) use the SAME
    // `rise` keyframe — so an unguarded handler would cut the shell's own
    // entrance short the first time any child animated.
    renderWithProviders(<App />, { route: '/chat' })

    const shell = screen.getByTestId('dashboard-shell')
    expect(shell).toHaveClass('animate-rise')

    const child = document.createElement('div')
    shell.appendChild(child)
    fireEvent.animationEnd(child, { animationName: 'rise' })

    expect(shell).toHaveClass('animate-rise')
  })

  it('keeps the shell entrance applied for an unrelated keyframe on the shell', () => {
    renderWithProviders(<App />, { route: '/chat' })

    const shell = screen.getByTestId('dashboard-shell')
    fireEvent.animationEnd(shell, { animationName: 'fade-in' })

    expect(shell).toHaveClass('animate-rise')
  })

  it('retires the shell entrance even when the animation is interrupted', () => {
    // An INTERRUPTED animation fires `animationcancel`, not `animationend`, and
    // React 18 exposes no handler for it — so hiding the pane inside the 350ms
    // entrance window would strand the class and replay it once. The timer
    // backstop must latch regardless of any animation event arriving.
    vi.useFakeTimers()
    try {
      renderWithProviders(<App />, { route: '/chat' })

      const shell = screen.getByTestId('dashboard-shell')
      expect(shell).toHaveClass('animate-rise')

      act(() => { vi.advanceTimersByTime(600) })

      expect(shell).not.toHaveClass('animate-rise')
    } finally {
      vi.useRealTimers()
    }
  })

  it('hosts the collapse control in the nav menu row and hides the Main group heading', () => {
    localStorage.removeItem('mc-nav')
    renderWithProviders(<App />, { route: '/chat' })

    const nav = screen.getByRole('navigation', { name: 'Main navigation' })
    // Brand (logo + name) now lives in the rail's menu row, replacing the old
    // hamburger; the collapse control is an arrow-left-to-line button.
    expect(within(nav).getByText('Crew')).toBeInTheDocument()
    const collapse = within(nav).getByRole('button', { name: 'Collapse sidebar' })
    expect(within(nav).queryByRole('button', { name: 'Toggle sidebar' })).not.toBeInTheDocument()
    expect(within(nav).queryByText('Main')).not.toBeInTheDocument()

    fireEvent.click(collapse)
    // Collapsed: the brand shrinks to a clickable logo that expands the rail;
    // the collapse control unmounts.
    expect(within(nav).getByRole('button', { name: 'Expand sidebar' })).toBeInTheDocument()
    expect(within(nav).queryByRole('button', { name: 'Collapse sidebar' })).not.toBeInTheDocument()
    expect(localStorage.getItem('mc-nav')).toBe('1')
    localStorage.removeItem('mc-nav')
  })

  it('hides the community row when the sidebar is collapsed', () => {
    localStorage.removeItem('mc-nav')
    renderWithProviders(<App />, { route: '/chat' })
    const nav = screen.getByRole('navigation', { name: 'Main navigation' })
    const contact = within(nav).getByText('Star us')
    expect(contact).toBeVisible()
    fireEvent.click(within(nav).getByRole('button', { name: 'Collapse sidebar' }))
    // The row folds away (max-h-0 + opacity-0 + inert) instead of unmounting.
    const wrapper = contact.closest('[class*="max-h-0"]')
    expect(wrapper).not.toBeNull()
    expect(wrapper).toHaveAttribute('inert')
    localStorage.removeItem('mc-nav')
  })

  it('keeps Request a Feature visible in the header actions cluster in both sidebar states', () => {
    safeSetItem('mc-nav', '1')
    renderWithProviders(<App />, { route: '/chat' })

    // Request a Feature moved out of the brand region into its own pill in the
    // header's right-side actions cluster; it stays visible regardless of the
    // sidebar's collapsed/expanded state.
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

  it('keeps theme controls available from Settings', () => {
    renderWithProviders(<App />, { route: '/chat' })
    // Theme controls live in Settings > Display rather than the shell header.
    expect(screen.getByText('Settings')).toBeInTheDocument()
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
