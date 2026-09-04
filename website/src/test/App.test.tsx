import { afterAll, describe, it, expect, vi } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { MOBILE_BREAKPOINT } from '../hooks/useIsMobile'
import { join } from 'node:path'
import { render, screen, act, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import App from '../App'
import { sseConnected, sseDisconnected } from '../store/dashboardSlice'
import { openActivityPanel, sseSubagentQueued } from '../store/chatSlice'
import SegmentedControl from '../components/SegmentedControl'
import { ApiError } from '../api/client'
import { safeSetItem } from '../utils/safeStorage'
import { FEATURE_REQUEST_PROMPT_FALLBACK } from '../prompts/featureRequest'

/** A failure `POST /api/chat/slots/{slot}/agent` really can return today. */
const REAL_FAILURE = 'invalid agent name'

/** Chrome the side tracks never get: the header's own `pl-2 pr-3` (20px) plus the
 *  two 12px track gaps. Subtracted before reasoning about a group's width —
 *  leaving the padding out is what first put the width factor 2vw too high. */
const TOPBAR_GAPS = 44

/** `clamp(240px, 22vw, 480px)` evaluated in JS. One definition, because three
 *  assertions below reason about it and three copies would drift apart. The
 *  literal it mirrors is pinned by the track-contract test. */
const searchWidth = (w: number) => Math.min(480, Math.max(240, w * 0.22))

/** The top-bar layout is a stylesheet contract (see `.topbar` in index.css):
 *  jsdom applies no CSS, so these read the rule text rather than computed style,
 *  which would pass against an empty rule. */
function topbarCss(): string {
  return readFileSync(join(__dirname, '..', 'index.css'), 'utf8')
}
/** The three declared tracks of the header grid, whitespace-normalised. */
function topbarTracks(): { sides: string[]; search: string } {
  const rule = topbarCss().match(/\.topbar\{[^}]*\}/)?.[0] ?? ''
  const cols = rule.match(/grid-template-columns:([^;}]+)/)?.[1].trim() ?? ''
  // Split on top-level spaces only: clamp()/minmax() contain spaces of their own.
  const parts: string[] = []
  let depth = 0
  let cur = ''
  for (const ch of cols) {
    if (ch === '(') depth++
    if (ch === ')') depth--
    if (ch === ' ' && depth === 0) { if (cur) parts.push(cur); cur = '' } else cur += ch
  }
  if (cur) parts.push(cur)
  return { sides: [parts[0], parts[2]], search: parts[1] }
}

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
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { credits_used: 3044, credits_covered: 3044, credits_overage: 0, credits_plan: 10000, resets: '2026-07-01', plan: 'KIRO POWER', cost_usd: 0, overage_rate: '0.04', bonus_credits: [{ name: 'Launch bonus', used: 250, total: 1000, days_left: 30 }], email: 'owner@example.com', account_type: 'Social' } }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
    themes: vi.fn().mockResolvedValue({ themes: [] }),
    themeDetail: vi.fn().mockResolvedValue({}),
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
    createChatSlot: vi.fn().mockResolvedValue({ key: 'feature-slot', title: 'feature-slot', messages: 0, running: false }),
    chatSlotContext: vi.fn().mockResolvedValue({ ok: true }),
    sendChat: vi.fn().mockResolvedValue({ ok: true }),
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
    // Keep the import chapter open after its scan. An empty scan deliberately
    // auto-completes the chapter, so asserting on the transient dialog races
    // that completion under a loaded test shard.
    vi.mocked(api.onboardingImportScan).mockResolvedValueOnce({
      sources: [{
        id: 'codex',
        name: 'Codex',
        detected: true,
        detail: '~/.codex',
        categories: [{
          id: 'instructions',
          label: 'Instructions',
          count: 1,
          description: 'Agent instructions',
        }],
      }],
      skipped: [],
      merge_only: true,
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

    afterAll(async () => {
      // Restore the default fully-onboarded mock so subsequent describe blocks
      // don't inherit a first-run state that renders the Privacy chapter.
      const { api } = await import('../api/client')
      vi.mocked(api.themeBoot).mockResolvedValue({
        mode: '',
        color: '',
        onboarded: true,
        import_onboarded: true,
        privacy_acked: true,
      } as never)
    })

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

  it('sizes the app shell in dvh so mobile browser chrome cannot cover the bottom row', () => {
    renderWithProviders(<App />, { route: '/chat' })
    // happy-dom does no layout, so the utility pair itself is pinned: h-dvh
    // tracks the visible viewport where dvh is supported, h-screen (100vh,
    // which extends under collapsible mobile browser UI) is the fallback.
    const shell = screen.getByTestId('dashboard-shell').closest('.h-screen')
    expect(shell).not.toBeNull()
    expect(shell!.className).toContain('supports-[height:100dvh]:h-dvh')
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
    // PR1 App Store split: the single 'Explore' entry is gone — the sidebar
    // now carries TWO App Store rows, Discover (/apps) and Library
    // (/apps/library).
    expect(screen.getByText('Discover')).toBeInTheDocument()
    expect(screen.getByText('Library')).toBeInTheDocument()
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

  it('renders the registry-derived Artifacts nav item, without a Knowledge rail item', () => {
    // Regression guard for the aaf7cfe stale-branch merge, which reverted the
    // registry-driven rail (`NAV_ITEMS = getBuiltinSurfaces().map(...)`) back
    // to a hardcoded array that omitted Artifacts. Artifacts is registered
    // unconditionally in `surfaces/builtins.tsx`, so it must always appear in
    // the rail. Knowledge is the opposite pin: it deliberately has NO rail
    // item — it lives as a tab inside Agent Capabilities and /knowledge
    // redirects there — so a rail entry reappearing is itself a regression.
    renderWithProviders(<App />, { route: '/chat' })
    expect(screen.getByText('Artifacts')).toBeInTheDocument()
    expect(screen.queryByText('Knowledge')).not.toBeInTheDocument()
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

  it('invalidates the registry query on mc:apps-changed so install state refreshes', async () => {
    // The Explore shelf renders Get vs Installed from the server-computed
    // `installed` flag on the `['registry']` rows, cached with a multi-minute
    // staleTime. Install/uninstall surfaces announce themselves via
    // mc:apps-changed; the handler must drop that cache or a just-installed
    // registry app keeps showing a "Get" button until the cache expires.
    const { queryClient } = renderWithProviders(<App />, { route: '/chat' })
    queryClient.setQueryData(['registry'], { apps: [] })
    expect(queryClient.getQueryState(['registry'])?.isInvalidated).toBe(false)
    act(() => { window.dispatchEvent(new Event('mc:apps-changed')) })
    await waitFor(() => {
      expect(queryClient.getQueryState(['registry'])?.isInvalidated).toBe(true)
    })
  })

  it('marks the apps cache stale on mc:apps-changed even when the refetch fails', async () => {
    // Dispatch sites do not invalidate ['apps'] themselves; this listener
    // owns that cache. refreshAppNav publishes fresh data only on fetch
    // SUCCESS, so the handler must invalidate the cache up front — otherwise
    // a retry-exhausted refetch chain would leave stale ['apps'] rows marked
    // fresh.
    const { api } = await import('../api/client')
    const listApps = api.listApps as ReturnType<typeof vi.fn>
    listApps.mockReset()
    listApps.mockRejectedValue(new Error('gateway down'))
    try {
      const { queryClient } = renderWithProviders(<App />, { route: '/chat' })
      queryClient.setQueryData(['apps'], [])
      expect(queryClient.getQueryState(['apps'])?.isInvalidated).toBe(false)
      act(() => { window.dispatchEvent(new Event('mc:apps-changed')) })
      await waitFor(() => {
        expect(queryClient.getQueryState(['apps'])?.isInvalidated).toBe(true)
      })
    } finally {
      listApps.mockReset()
      listApps.mockResolvedValue([])
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

  it('omits sub-agent activity from the collapsed Sessions rail item', async () => {
    localStorage.setItem('mc-nav', '1')
    const store = createTestStore()
    store.dispatch(sseSubagentQueued({ slot: 'background', queued: 2 }))

    renderWithProviders(<App />, { route: '/chat', store })

    expect(await screen.findByLabelText('Sessions')).toBeInTheDocument()
    expect(screen.queryByLabelText('2 subagents in flight')).not.toBeInTheDocument()
  })

  it('keeps the sub-agent bot and count in the expanded Sessions rail item', async () => {
    localStorage.removeItem('mc-nav')
    const store = createTestStore()

    renderWithProviders(<App />, { route: '/chat', store })

    // Seed AFTER the mount fetch settles. `fetchSlots.fulfilled` is an
    // authoritative slot-list writer, so queued-subagent state for a slot the
    // fetched list does not name is residue and is evicted — seeding before the
    // fetch would have this test depend on that eviction not happening.
    expect(await screen.findByLabelText('Sessions')).toBeInTheDocument()
    act(() => { store.dispatch(sseSubagentQueued({ slot: 'background', queued: 2 })) })

    expect(await screen.findByLabelText('2 subagents in flight')).toBeInTheDocument()
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

  it('uses installed theme branding in the left rail and browser favicon', async () => {
    const { api } = await import('../api/client')
    localStorage.removeItem('mc-nav')
    localStorage.setItem('mc-color-theme', 'custom-pearce')
    vi.mocked(api.themes).mockResolvedValueOnce({
      themes: [{ slug: 'pearce', name: 'Pearce CRT', emoji: 'PC', source: 'installed' }],
    } as never)
    vi.mocked(api.themeDetail).mockResolvedValueOnce({
      slug: 'pearce',
      name: 'Pearce CRT',
      emoji: 'PC',
      level: 1,
      source: 'installed',
      dark: { '--bg': '#000', '--text': '#fff', '--accent': '#fc0' },
      light: { '--bg': '#fff', '--text': '#000', '--accent': '#840' },
      assets: {
        branding: {
          botName: 'KIRO CREW',
          logo: 'branding/logo.svg',
          favicon: 'branding/favicon.svg',
        },
      },
    } as never)

    const view = renderWithProviders(<App />, { route: '/chat' })
    try {
      const nav = screen.getByRole('navigation', { name: 'Main navigation' })
      const brand = within(nav).getByRole('button', { name: 'Collapse sidebar' })
      await waitFor(() => expect(brand).toHaveTextContent('KIRO CREW'))
      expect(brand.querySelector('img')).toHaveAttribute(
        'src',
        '/api/theme/pearce/assets/branding/logo.svg',
      )
      const favicon = document.getElementById('mc-theme-favicon') as HTMLLinkElement | null
      expect(favicon).not.toBeNull()
      expect(favicon).toHaveAttribute(
        'href',
        '/api/theme/pearce/assets/branding/favicon.svg',
      )
    } finally {
      view.unmount()
      document.getElementById('mc-theme-favicon')?.remove()
      document.documentElement.style.removeProperty('--theme-logo')
      localStorage.removeItem('mc-color-theme')
      localStorage.removeItem('mc-nav')
    }
  })

  it('opens Search Everywhere from the theme-aware shadowless header trigger', () => {
    renderWithProviders(<App />, { route: '/chat' })
    const trigger = screen.getByRole('button', { name: 'Search sessions, files, and commands' })
    expect(trigger).toHaveClass('rounded-md', 'border-border', 'bg-card', 'shadow-none')
    expect(trigger).not.toHaveClass('rounded-full')
    fireEvent.click(trigger)
    expect(screen.getByRole('dialog', { name: 'Search everywhere' })).toBeInTheDocument()
  })

  it('renders the search trigger in the header centre track, not as a positioned overlay', () => {
    renderWithProviders(<App />, { route: '/chat' })
    const trigger = screen.getByRole('button', { name: 'Search sessions, files, and commands' })
    // The trigger is a flow item now: it fills its grid track and carries no
    // positioning of its own. The previous implementation centred it on `50vw`
    // with a JS-measured inline width, which is what forced it to reserve
    // `max(left, right)` on BOTH sides and drop itself once that mirrored gutter
    // fell under a floor.
    //
    // It shares the centre CELL with the focus-mode toggle, so it fills that
    // cell (`flex-1`) rather than the track directly — the cell is what fills
    // the track. Both halves of the original assertion still hold: nothing here
    // is positioned, and the header keeps exactly three in-flow children.
    expect(trigger).toHaveClass('flex-1')
    expect(trigger).not.toHaveClass('absolute')
    expect(trigger.style.left).toBe('')
    expect(trigger.style.width).toBe('')
    // Header children, in order: left group · centre cell · actions group. The
    // three-track grid depends on that being exactly three in-flow children.
    const centre = trigger.parentElement!
    const header = centre.parentElement!
    const flow = [...header.children].filter(el => !el.className.includes('absolute'))
    expect(flow[0]).toHaveClass('tb-left')
    expect(flow[1]).toBe(centre)
    expect(flow[2]).toHaveClass('tb-right')
    // The centre cell holds the trigger and the focus-mode toggle, and nothing
    // else: a third control there is what website/AUTOSDE.yaml's
    // max-two-buttons-per-row rule forbids.
    expect([...centre.children]).toHaveLength(2)
    expect(centre.children[1]).toBe(screen.getByTestId('focus-mode-toggle'))
  })

  it('sizes the top-bar search from the window alone, with equal side tracks', () => {
    // The layout lives in the stylesheet, so the contract is asserted there:
    // jsdom applies no CSS, and a computed-style assertion would pass against
    // an empty rule. Equal side tracks are what make the search exactly
    // window-centred without measuring anything.
    const { search, sides } = topbarTracks()
    expect(search).toBe('clamp(240px, 22vw, 480px)')
    expect(sides).toEqual(['minmax(0,1fr)', 'minmax(0,1fr)'])
  })

  it('leaves every desktop width room for the actions group icons-only form', () => {
    // The invariant that replaced the per-track floor: the side tracks are pure
    // remainder, so the search width function is the only thing standing between
    // a narrow window and a clipped actions group. 139px is the measured
    // icons-only width of that group (capture/topbar-search-variants.tsx).
    const ICONS_ONLY = 139
    for (let w = 768; w <= 3840; w += 8) {
      const perSide = (w - searchWidth(w) - TOPBAR_GAPS) / 2
      expect(perSide).toBeGreaterThanOrEqual(ICONS_ONLY)
    }
  })

  it('keeps the live readouts at the modal desktop widths', () => {
    // The point of the width function, and the reason it is 22vw rather than
    // something roomier: the widest readout tier measures 518px and its rung
    // fires at 530px, so the side track has to clear 531 or the numbers are
    // evicted. The layout this replaces showed full readouts at these widths, so
    // evicting them would be a regression traded for a bigger inert trigger.
    const READOUT_RUNG = 530
    for (const w of [1440, 1600, 1920, 2560]) {
      const perSide = (w - searchWidth(w) - TOPBAR_GAPS) / 2
      expect(perSide).toBeGreaterThan(READOUT_RUNG)
    }
  })

  it('never makes the search wider than the mirrored-gutter layout above 1376px', () => {
    // "Equal or narrower" is the constraint on this redesign: the space reclaimed
    // from the mirrored gutter goes to the side groups, not into a bigger
    // trigger. That holds from 1376px up, where the old geometry's own box was
    // still growing.
    //
    // The exception is the 1304-1370 band, asserted separately below: there the
    // old layout had already been squeezed to its 240px floor (one pixel narrower
    // and it unmounted the trigger entirely), so "narrower than old" would mean
    // capping the new box at 240 up to 1370 -- a ~17vw factor that would hand the
    // side groups far more room than their content can use.
    const oldWidth = (w: number) => {
      const gutter = Math.ceil(Math.max(187, 520)) + 12   // measured clusters
      return Math.max(240, Math.min(Math.round(w / 3) - 40, w - gutter * 2))
    }
    for (let w = 1376; w <= 3840; w += 8) {
      expect(searchWidth(w)).toBeLessThanOrEqual(oldWidth(w))
    }
  })

  it('stays within the old floor-to-cap envelope in the band where the old box was pinned', () => {
    // 1304 (the old layout's visibility threshold) to ~1370 (where the two cross).
    // The old box was pinned at 240 across the whole band; the new one grows from
    // 287 to 301, so it is wider -- bounded, and against a box the old design was
    // about to drop rather than one it was rendering comfortably.
    for (const w of [1304, 1336, 1368]) {
      expect(searchWidth(w)).toBeLessThanOrEqual(301)
    }
  })

  it('gives each side group its own size-query container and container-keyed rungs', () => {
    // Each group re-lays-out against the width IT was handed, which a viewport
    // media query cannot know: the actions group's content varies with the usage
    // pill, resource posture and registered extension segments.
    const css = topbarCss()
    expect(css).toMatch(/\.tb-left,\s*\.tb-right\{[^}]*container-type:\s*inline-size/)
    for (const rung of ['tb-drop-metrics', 'tb-drop-usage', 'tb-drop-feedback', 'tb-narrow-only']) {
      expect(css).toMatch(new RegExp(`@container \\([^)]+\\)\\{\\s*\\.${rung}\\{`))
    }
  })

  it('shifts the collapse rungs by the pill footprint of the matching viewport base', () => {
    // The update pill is a conditional sibling of the ladder: it never shrinks
    // and only exists while an update does, so the rung budget has extra bases
    // while it is mounted — and the pill's own label is viewport-gated
    // (`hidden sm:inline`, 640px), so the shift exists ONLY where the label
    // does: the widest shipped-locale label form at ≥640px, and NOTHING below.
    // A phone hands the right group ≤240px routinely, so ANY shifted terminal
    // rung there blanks the CPU/MEM/DSK and credits readouts for the whole
    // time an update is pending (#7698); the 200–240px squeeze band with the
    // icon-only pill degrades to the segments' nowrap leading-edge clip
    // instead, which is the recoverable harm.
    // Constants are measured in
    // capture/topbar-search-variants.tsx (?update=on&updatelabel=…); the
    // dev-only en-XA pseudolocale is excluded (nowrap backstop covers it).
    // Re-measure and update BOTH the constants here and the index.css rungs
    // when the pill's chrome or any locale catalog changes its widest form —
    // the catalog-drift test below fails when that happens.
    const css = topbarCss().replace(/\/\*[\s\S]*?\*\//g, '')
    // Brace-balanced extraction: the ≥640px rungs live inside
    // `@media (min-width:640px)` blocks, which nest @container blocks, so a
    // lazy regex to the first `}` cannot delimit them.
    const mediaBlocks: string[] = []
    let rest = ''
    let cursor = 0
    const opener = /@media \(min-width:640px\)\{/g
    let m: RegExpExecArray | null
    while ((m = opener.exec(css)) !== null) {
      let depth = 1
      let i = opener.lastIndex
      while (i < css.length && depth > 0) {
        if (css[i] === '{') depth++
        else if (css[i] === '}') depth--
        i++
      }
      mediaBlocks.push(css.slice(opener.lastIndex, i - 1))
      rest += css.slice(cursor, m.index)
      cursor = i
      opener.lastIndex = i
    }
    rest += css.slice(cursor)
    const desktopCss = mediaBlocks.join('\n')

    const rung = (scope: string, selector: string, hasUpdate: boolean, label: string): number => {
      const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
      // Anchored to line start: without it a same-suffix rule (e.g. a scoped
      // `.tb-has-update .tb-capsule …` line) could satisfy the base lookup and
      // pair the shift assertion against the wrong rung.
      const re = new RegExp(
        `^\\s*@container \\(max-width:(\\d+)px\\)\\{ ?${hasUpdate ? '\\.tb-has-update ' : ''}${esc}\\{display:none\\}`,
        'm'
      )
      const match = scope.match(re)
      expect(match, `expected ${label} rung for ${selector}`).not.toBeNull()
      return Number(match![1])
    }
    const PILL_WIDEST_LABELED = 201.7 // de downloading_percent "Wird heruntergeladen 100 %"
    const GROUP_GAP = 6
    const SHIFT_LABELED = Math.ceil(PILL_WIDEST_LABELED + GROUP_GAP)
    const TERMINAL = '.tb-capsule > *:not(:first-child)'
    // ≥640px (label visible): every rung, terminal included, shifts by the
    // labeled footprint, inside the media gate.
    for (const sel of ['.tb-drop-metrics', '.tb-drop-usage', '.tb-drop-feedback', TERMINAL]) {
      expect(
        rung(desktopCss, sel, true, '≥640 shifted') - rung(rest, sel, false, 'base'),
        `labeled shift for ${sel}`
      ).toBe(SHIFT_LABELED)
    }
    // <640px: NO `.tb-has-update` rung of any kind outside the media gate.
    // THE regression guard for #7698: an unscoped shifted terminal rung
    // (240px) applied on phones, where the right group is routinely ≤240px,
    // so it blanked the CPU/MEM/DSK and credits readouts the whole time an
    // update was pending. Below 640px the icon-only pill's footprint is
    // absorbed by the segments' nowrap leading-edge clip and the base 200px
    // terminal rung bounds the capsule.
    expect(
      rest.match(/@container \([^)]*\)\{\s*\.tb-has-update /),
      'no tb-has-update rung may exist outside the ≥640px media gate'
    ).toBeNull()
    // …and the base terminal rung must still bound the phone form.
    expect(rung(rest, TERMINAL, false, 'base')).toBeGreaterThan(0)
    // The metric readout's icon stand-in must stay visible through the shifted
    // band, inside the same media gate: the base rule hides it from 531px up,
    // so the counterpart re-shows it between the base metrics rung and the
    // shifted one.
    const iconBand = desktopCss.match(
      /@container \(min-width:(\d+)px\) and \(max-width:(\d+)px\)\{\s*\.tb-has-update \.tb-narrow-only\{display:(?!none)/
    )
    expect(iconBand, 'expected the tb-has-update .tb-narrow-only counterpart band').not.toBeNull()
    expect(Number(iconBand![1])).toBeLessThanOrEqual(rung(rest, '.tb-drop-metrics', false, 'base') + 1)
    expect(Number(iconBand![2])).toBe(rung(desktopCss, '.tb-drop-metrics', true, '≥640 shifted'))
  })

  it('fails when a locale catalog outgrows the measured pill budget', () => {
    // The 201.7px constant above is a hand-measured number, so a catalog change
    // that makes some other label the widest would leave the budget silently
    // stale (degraded to a clean clip by the nowrap backstop, but stale).
    // jsdom cannot measure rendered text, so the sentinel compares WIDTH UNITS:
    // East-Asian wide/fullwidth glyphs count 2, everything else 1 — a CJK glyph
    // renders ~2x a Latin one at this font size, so a 15-char Japanese label
    // that would out-render the 26-char German one trips the guard instead of
    // hiding behind a smaller .length. The range list is a BMP approximation
    // (supplementary-plane CJK and emoji count 1, combining marks count 1
    // each); it is a drift tripwire, not a width oracle — the real number
    // always comes from re-measuring in the harness. The anchor is the MEASURED string
    // itself: it must still exist, and no shipped visible label may exceed its
    // unit width. When this fails, re-measure with
    // capture/topbar-search-variants.tsx and update the constants + rungs.
    const wide = /[\u1100-\u115F\u2E80-\u303E\u3041-\u33FF\u3400-\u4DBF\u4E00-\u9FFF\uA000-\uA4CF\uAC00-\uD7A3\uF900-\uFAFF\uFE30-\uFE4F\uFF00-\uFF60\uFFE0-\uFFE6]/
    const unitWidth = (s: string): number =>
      [...s].reduce((acc, ch) => acc + (wide.test(ch) ? 2 : 1), 0)
    const localesDir = join(__dirname, '..', 'i18n', 'locales')
    const MEASURED = 'Wird heruntergeladen 100 %'
    const visibleKeys = ['update_available', 'update_ready', 'downloading', 'downloading_percent']
    let measuredSeen = false
    let localesWithPill = 0
    for (const file of readdirSync(localesDir).filter(f => f.endsWith('.json'))) {
      if (file === 'en-XA.json') continue // devOnly pseudolocale, excluded from the budget
      const pill = JSON.parse(readFileSync(join(localesDir, file), 'utf8')).components?.updatePill
      if (!pill) continue
      localesWithPill++
      for (const key of visibleKeys) {
        const label = (pill[key] ?? '').replace('{{percent}}', '100')
        if (label === MEASURED) measuredSeen = true
        expect(
          unitWidth(label),
          `${file} ${key} "${label}" out-measures the widest pill label the budget was derived from — re-measure`
        ).toBeLessThanOrEqual(unitWidth(MEASURED))
      }
    }
    // Guards the sentinel itself: a renamed i18n namespace would otherwise make
    // every lookup miss and the loop above pass vacuously.
    expect(localesWithPill, 'expected the shipped catalogs to carry updatePill labels').toBeGreaterThanOrEqual(10)
    expect(measuredSeen, 'the measured widest label no longer exists — re-measure the budget').toBe(true)
  })

  it('keeps the desktop form switch at or above the pill label gate', () => {
    // The <640px rung base in index.css shifts ONLY the capsule's terminal
    // rung, on the premise that the named readout rungs render desktop-only
    // elements and the desktop layout never exists below the pill's own label
    // gate (`hidden sm:inline`, 640px). That premise is this inequality; if
    // the form switch ever drops below the gate, the 531-640px band would pair
    // full desktop readouts with an unbudgeted icon-only pill.
    expect(MOBILE_BREAKPOINT).toBeGreaterThanOrEqual(640)
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

  it('lets the brand toggle expand the rail while preview expand mode is active', () => {
    localStorage.removeItem('mc-nav')
    renderWithProviders(<App />, { route: '/chat' })
    const nav = screen.getByRole('navigation', { name: 'Main navigation' })

    // Entering the Web Preview's expand mode collapses the rail.
    act(() => {
      window.dispatchEvent(new CustomEvent('kirocrew-preview-expand', { detail: { expanded: true } }))
    })
    expect(within(nav).getByRole('button', { name: 'Expand sidebar' })).toBeInTheDocument()

    // The logo keeps its standard behavior inside expand mode: it expands.
    fireEvent.click(within(nav).getByRole('button', { name: 'Expand sidebar' }))
    expect(within(nav).getByRole('button', { name: 'Collapse sidebar' })).toBeInTheDocument()

    // Leaving expand mode must not undo that explicit choice.
    act(() => {
      window.dispatchEvent(new CustomEvent('kirocrew-preview-expand', { detail: { expanded: false } }))
    })
    expect(within(nav).getByRole('button', { name: 'Collapse sidebar' })).toBeInTheDocument()
    localStorage.removeItem('mc-nav')
  })

  it('restores the pre-expand rail state when preview expand mode ends untouched', () => {
    localStorage.removeItem('mc-nav') // start expanded
    renderWithProviders(<App />, { route: '/chat' })
    const nav = screen.getByRole('navigation', { name: 'Main navigation' })
    expect(within(nav).getByRole('button', { name: 'Collapse sidebar' })).toBeInTheDocument()

    act(() => {
      window.dispatchEvent(new CustomEvent('kirocrew-preview-expand', { detail: { expanded: true } }))
    })
    expect(within(nav).getByRole('button', { name: 'Expand sidebar' })).toBeInTheDocument()

    act(() => {
      window.dispatchEvent(new CustomEvent('kirocrew-preview-expand', { detail: { expanded: false } }))
    })
    expect(within(nav).getByRole('button', { name: 'Collapse sidebar' })).toBeInTheDocument()
    // The auto-collapse is transient: it never writes the persisted preference.
    expect(localStorage.getItem('mc-nav')).toBeNull()
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

  it('keeps feature-request instructions hidden from the persisted user message', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.createChatSlot).mockClear()
    vi.mocked(api.chatSlotContext).mockClear()
    vi.mocked(api.sendChat).mockClear()
    renderWithProviders(<App />, { route: '/chat' })

    fireEvent.click(screen.getByRole('button', { name: 'Request a Feature' }))

    await waitFor(() => {
      expect(api.chatSlotContext).toHaveBeenCalledWith(
        'feature-slot',
        FEATURE_REQUEST_PROMPT_FALLBACK,
        // maxAge bounds the hidden seed's lifetime so a failed visible send
        // cannot leave it queued for a later, unrelated message.
        { source: 'feature-request', maxAge: 60 },
      )
      expect(api.sendChat).toHaveBeenCalledWith(
        'I’d like to request a feature!',
        'feature-slot',
        expect.any(String),
      )
    })
    expect(api.sendChat).not.toHaveBeenCalledWith(
      FEATURE_REQUEST_PROMPT_FALLBACK,
      expect.anything(),
      expect.anything(),
    )
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

describe('mobile nav drawer insets', () => {
  /** The drawer's className, read from source: it only renders below 768px and
   *  jsdom applies no CSS, so a rendered assertion here would either need the
   *  whole mobile shell stood up or would pass against an empty rule. */
  function mobileDrawerClasses(): string[] {
    const src = readFileSync(join(__dirname, '..', 'App.tsx'), 'utf8')
    const drawer = src.slice(src.indexOf('key="mobile-nav-drawer"'))
    const cls = drawer.match(/className="([^"]+)"/)?.[1] ?? ''
    expect(cls, 'expected to find the mobile nav drawer className').not.toBe('')
    return cls.split(/\s+/)
  }

  it('insets all four sides equally', () => {
    // The drawer is `fixed` to the VIEWPORT, not placed in the grid row below
    // the topbar the way the desktop rail is, so it owns its own top offset.
    // Without it the card's rounded top edge sits flat against the screen while
    // the other three sides float — see the reported defect.
    const classes = mobileDrawerClasses()
    expect(classes).toContain('mx-2')
    expect(classes).toContain('mt-2')
    expect(classes).toContain('mb-2')
    expect(classes).not.toContain('mt-0')
  })

  it('spans the viewport height so both margins resolve', () => {
    // An anchor on BOTH ends plus a margin on each resolves the height to
    // viewport-16px. Dropping either anchor would make the margins inert (auto
    // height) and re-open the flush-top defect from the other direction.
    //
    // The safe-area variants satisfy this the same way the plain ones do: they
    // set top/bottom to env(safe-area-inset-*), a definite length that is 0 on
    // hardware without a notch. So accept either form per end, but keep
    // requiring that BOTH ends are anchored -- that is the actual invariant,
    // and the literal class name is not.
    const classes = mobileDrawerClasses()
    expect(classes).toContain('fixed')
    expect(classes.some(c => c === 'top-0' || c === 'top-safe'), `expected a top anchor, got: ${classes.join(' ')}`).toBe(true)
    expect(classes.some(c => c === 'bottom-0' || c === 'bottom-safe'), `expected a bottom anchor, got: ${classes.join(' ')}`).toBe(true)
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

  it('renders "MEM —" instead of crashing when mem_used_gb is missing but mem_total_gb is present', async () => {
    const { api } = await import('../api/client')
    const sysMock = vi.mocked(api.system)
    // The shape that crashed the root app-shell boundary with
    // "Cannot read properties of undefined (reading 'toFixed')":
    // `_collect_system_metrics` seeds the frame from the CACHED static system
    // info (which carries mem_total_gb) and then computes mem_used_gb/
    // mem_free_gb under `try/except: pass`, so a failed memory probe yields a
    // total with no used. A `memTotal > 0` gate admits that frame and then
    // formats `undefined.toFixed(1)`.
    sysMock.mockResolvedValueOnce({ mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 } as never)
    localStorage.setItem('mc-topbar-metrics', '1')
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByText(/MEM —/)).toBeInTheDocument()
    // The rest of the same frame still renders — one absent probe must not
    // blank the whole capsule, let alone unmount the app.
    expect(screen.getByText(/CPU 25%/)).toBeInTheDocument()
    expect(screen.getByText(/DSK 40%/)).toBeInTheDocument()
    sysMock.mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 } as never)
    localStorage.removeItem('mc-topbar-metrics')
  })

  it('renders "DSK —" instead of NaN when disk_free_gb is missing but disk_total_gb is present', async () => {
    const { api } = await import('../api/client')
    const sysMock = vi.mocked(api.system)
    sysMock.mockResolvedValueOnce({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0 } as never)
    localStorage.setItem('mc-topbar-metrics', '1')
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByText(/DSK —/)).toBeInTheDocument()
    expect(screen.getByText(/MEM 25%/)).toBeInTheDocument()
    sysMock.mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 } as never)
    localStorage.removeItem('mc-topbar-metrics')
  })

  it('renders every readout as "—" instead of crashing when the frame carries non-finite numbers', async () => {
    const { api } = await import('../api/client')
    const sysMock = vi.mocked(api.system)
    // NaN/Infinity reach the frame when a probe divides by an unmeasured total;
    // they must take the placeholder path, not render "NaN%".
    sysMock.mockResolvedValueOnce({ mem_used_gb: NaN, mem_total_gb: 16.0, cpu_pct: NaN, disk_total_gb: Infinity, disk_free_gb: 60.0 } as never)
    localStorage.setItem('mc-topbar-metrics', '1')
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByText(/CPU —/)).toBeInTheDocument()
    expect(screen.getByText(/MEM —/)).toBeInTheDocument()
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
    // The switch now rides performSlotSwitch (#5120), so the API call lands a
    // microtask after the keydown — flush with an async act.
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'A', code: 'KeyA', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotAgent).toHaveBeenCalledWith('slot-1', 'reviewer')
    // The pick must land in the store WITHOUT a slots round trip (#5120):
    // no websocket exists in this harness, so only the optimistic write can
    // move the row. The mock resolves {} — the requested-name fallback path.
    await waitFor(() => expect(
      store.getState().dashboard.slots.find((s: { key: string }) => s.key === 'slot-1')?.agent,
    ).toBe('reviewer'))
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

  it.each([
    ['forward', 'A', 'KeyA', 'reviewer'],
    ['backward', 'Z', 'KeyZ', 'oracle'],
  ])('surfaces an agent-switch API failure when cycling %s', async (_direction, key, code, expectedAgent) => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotAgent as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new ApiError(400, REAL_FAILURE, JSON.stringify({ error: REAL_FAILURE })),
    )
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: true, agent: 'kirocrew' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })

    // The switch now rides performSlotSwitch (#5120): flush the microtask
    // chain so both the API call and the failure notice land.
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key, code, altKey: true, shiftKey: true, bubbles: true }))
    })

    expect(api.chatSlotAgent).toHaveBeenCalledWith('slot-1', expectedAgent)
    const noticeText = await screen.findByText(
      REAL_FAILURE,
    )
    expect(noticeText.closest('[role="status"]')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByText(
      REAL_FAILURE,
    )).not.toBeInTheDocument()
  })

  it('restarts the six-second expiry after a repeated failure', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      const failure = new ApiError(400, REAL_FAILURE, JSON.stringify({ error: REAL_FAILURE }))
      ;(api.chatSlotAgent as ReturnType<typeof vi.fn>).mockRejectedValue(failure)
      store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: true, agent: 'kirocrew' }] })
      store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
      renderWithProviders(<App />, { route: '/chat' })

      await act(async () => {
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'A', code: 'KeyA', altKey: true, shiftKey: true, bubbles: true }))
        await Promise.resolve()
      })
      const copy = REAL_FAILURE
      expect(screen.getByText(copy)).toBeInTheDocument()

      await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
      await act(async () => {
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'A', code: 'KeyA', altKey: true, shiftKey: true, bubbles: true }))
        await Promise.resolve()
      })
      await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
      expect(screen.getByText(copy)).toBeInTheDocument()

      await act(async () => { await vi.advanceTimersByTimeAsync(4500) })
      expect(screen.queryByText(copy)).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
      ;(api.chatSlotAgent as ReturnType<typeof vi.fn>).mockResolvedValue({})
    }
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

describe('onCycleReasoningEffort keyboard shortcut (#5120)', () => {
  it('steps a burst from the in-flight target and writes the adjudicated level', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotReasoningEffort as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, agent: 'kirocrew', reasoning_effort: 'max' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    // Two presses in one synchronous batch: the first pick is still in
    // flight when the second press computes its base. From 'max' the first
    // press targets '' (clear the override — a REAL target), so the second
    // press's base MUST come from pendingSlotSwitchTarget: reading the store
    // (still 'max', nothing settled) would issue '' twice, and the ''-falsy
    // accessor would misread the in-flight '' the same way.
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'D', code: 'KeyD', altKey: true, shiftKey: true, bubbles: true }))
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'D', code: 'KeyD', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotReasoningEffort).toHaveBeenNthCalledWith(1, 'slot-1', '')
    expect(api.chatSlotReasoningEffort).toHaveBeenNthCalledWith(2, 'slot-1', 'low')
    // The adjudicated survivor (the newest pick) lands in the store without
    // a slots round trip — no websocket exists in this harness.
    await waitFor(() => expect(
      store.getState().dashboard.slots.find((s: { key: string }) => s.key === 'slot-1')?.reasoning_effort,
    ).toBe('low'))
  })

  it('cycles backward on Alt+Shift+C and writes the store', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotReasoningEffort as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, agent: 'kirocrew', reasoning_effort: 'low' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'C', code: 'KeyC', altKey: true, shiftKey: true, bubbles: true }))
    })
    // 'low' is index 1; backward reaches '' (provider default).
    expect(api.chatSlotReasoningEffort).toHaveBeenCalledWith('slot-1', '')
    await waitFor(() => expect(
      store.getState().dashboard.slots.find((s: { key: string }) => s.key === 'slot-1')?.reasoning_effort,
    ).toBe(''))
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
    // Async act: the switch protocol (#5120) chains the wire call on a
    // microtask, so the mock is invoked a tick after the keydown.
    await act(async () => {
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
    const pill = await screen.findByTitle('Kiro credit usage')
    expect(pill).toBeInTheDocument()
  })

  it('renders the true total (credits_used) including overage above the plan', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.sessionsUsage).mockResolvedValueOnce({
      usage: { credits_covered: 10000, credits_used: 10500, credits_overage: 500, credits_plan: 10000, resets: '2026-07-01', plan: 'KIRO POWER' },
    } as never)
    renderWithProviders(<App />, { route: '/chat' })
    // credits_used=10500 total / 10000 plan = 105% (500 over plan)
    expect(await screen.findByTitle('Kiro credit usage')).toBeInTheDocument()
  })

  it('opens a details modal with breakdown rows when clicked', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    const pill = await screen.findByTitle('Kiro credit usage')
    fireEvent.click(pill)
    expect(await screen.findByRole('dialog', { name: 'Kiro Account' })).toBeInTheDocument()
    expect(await screen.findByText('owner@example.com')).toBeInTheDocument()
    expect(screen.getByText(/Signed in with Social login/)).toBeInTheDocument()
    expect(await screen.findByText('KIRO POWER')).toBeInTheDocument()
    expect(screen.getByText(/Resets/)).toBeInTheDocument()
    expect(screen.getByText(/Remaining credit balance: 6,956/)).toBeInTheDocument()
    expect(screen.getByText('Launch bonus')).toBeInTheDocument()
    expect(screen.getByText(/Remaining credit balance: 750/)).toBeInTheDocument()
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
    expect(await screen.findByLabelText('Checking credit usage')).toBeInTheDocument()
  })

  it('defaults covered/overage to 0 and renders sub-1000 values without K suffix', async () => {
    const { api } = await import('../api/client')
    // only credits_plan present -> credits_used falls back to 0
    vi.mocked(api.sessionsUsage).mockResolvedValueOnce({ usage: { credits_plan: 500 } } as never)
    renderWithProviders(<App />, { route: '/chat' })
    const pill = await screen.findByTitle('Kiro credit usage')
    expect(pill).toHaveTextContent('0/500') // sub-1000 -> no "K" formatting
    fireEvent.click(pill)
    expect(await screen.findByText('0 credits')).toBeInTheDocument() // Overage used row
  })

  it('handles a zero limit without dividing by zero (0%)', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.sessionsUsage).mockResolvedValueOnce({ usage: { credits_plan: 0, credits_covered: 0 } } as never)
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByTitle('Kiro credit usage')).toBeInTheDocument()
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
    const pill = await screen.findByTitle('Kiro credit usage')
    // Focus the pill first, as a real click does: focus restore is `Modal`'s
    // generic behaviour (it returns focus to whatever was focused when the
    // dialog opened), not a per-call-site `ref.focus()` in App.
    pill.focus()
    fireEvent.click(pill)
    expect(await screen.findByText('Overage used')).toBeInTheDocument()
    act(() => { fireEvent.keyDown(window, { key: 'Escape' }) })
    await waitFor(() => expect(screen.queryByText('Overage used')).not.toBeInTheDocument())
    await waitFor(() => expect(pill).toHaveFocus())
  })

  it('hides the pill entirely when usage is unavailable (non-Kiro provider)', async () => {
    const { api } = await import('../api/client')
    // Backend reports available:false when kiro-cli is absent (e.g. a Claude-only provider).
    vi.mocked(api.sessionsUsage).mockResolvedValue({ usage: { available: false } } as never)
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(screen.queryByTitle(/Kiro credit usage/)).not.toBeInTheDocument())
    expect(screen.queryByTitle('Kiro credit usage')).not.toBeInTheDocument()
  })

  it('auto-closes the modal if usage resolves to unavailable while it is open', async () => {
    const { api } = await import('../api/client')
    let resolveUsage: (v: unknown) => void = () => {}
    vi.mocked(api.sessionsUsage).mockReturnValue(new Promise(r => { resolveUsage = r }) as never)
    renderWithProviders(<App />, { route: '/chat' })
    const pill = await screen.findByTitle(/Kiro credit usage/)
    fireEvent.click(pill)
    expect(await screen.findByLabelText('Checking credit usage')).toBeInTheDocument()
    await act(async () => { resolveUsage({ usage: { available: false } }); await Promise.resolve() })
    await waitFor(() => expect(screen.queryByLabelText('Checking credit usage')).not.toBeInTheDocument())
  })

  it('never renders NaN when credit fields arrive non-finite', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.sessionsUsage).mockResolvedValue({ usage: { credits_plan: NaN, credits_used: NaN, credits_covered: NaN } } as never)
    renderWithProviders(<App />, { route: '/chat' })
    // Non-finite plan is rejected by the Number.isFinite guard, so the loaded
    // pill (which would otherwise show "NaN / NaN") never appears.
    await waitFor(() => expect(screen.queryByTitle('Kiro credit usage')).not.toBeInTheDocument())
    expect(screen.queryByText(/NaN/)).not.toBeInTheDocument()
  })
})
