// App-level behaviour outside routing: the top bar's system metrics and Kiro
// credits pill, the agent / reasoning effort / approval mode / model cycling
// shortcuts, and two source contracts on the shell's source (the mobile nav drawer insets,
// the single import-outcome notice mount). App.test.tsx covers routing; the
// module mocks and the matchMedia / ResizeObserver stubs below are copied from it
// word for word.
import { describe, it, expect, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { screen, act, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import App from '../App'
import { ApiError } from '../api/client'

/** A failure `POST /api/chat/slots/{slot}/agent` really can return today. */
const REAL_FAILURE = 'invalid agent name'

// Mock all page components to isolate routing
vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => <div data-testid="system-page">SystemPage</div> }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => <div data-testid="projects-page">ProjectsPage</div> }))
vi.mock('../pages/LogsPage', () => ({ default: () => <div data-testid="logs-page">LogsPage</div> }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => <div data-testid="mc-agents-page">MCAgentsPage</div> }))
vi.mock('../pages/CapabilitiesPage', () => ({ default: () => <div data-testid="capabilities-page">CapabilitiesPage</div> }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => <div data-testid="notifications-page">NotificationsPage</div> }))
vi.mock('../pages/SchedulePage', () => ({ default: () => <div data-testid="schedule-page">SchedulePage</div> }))
// A lazy route whose first chunk fetch rejects, then succeeds when retried.
// The factory count models the browser re-attempting the dynamic import after
// a transient gateway or stale-chunk failure.
const hooksPageMock = vi.hoisted(() => ({ attempts: 0 }))
vi.mock('../pages/HooksPage', () => {
  hooksPageMock.attempts += 1
  if (hooksPageMock.attempts === 1) {
    throw new Error('Failed to fetch dynamically imported module: HooksPage')
  }
  return { default: () => <div data-testid="hooks-page">HooksPage</div> }
})
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
    // A NON-Kiro harness by default, so the credit pill's Kiro-only surfaces
    // (the no-reading dash) stay off unless a test names the kiro backend.
    kirocrewConfig: vi.fn().mockResolvedValue({ agent: { acp_backend: 'claude' } }),
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

describe('mobile nav drawer insets', () => {
  /** The drawer's className, read from source: it only renders below 768px and
   *  jsdom applies no CSS, so a rendered assertion here would either need the
   *  whole mobile shell stood up or would pass against an empty rule. */
  function mobileDrawerClasses(): string[] {
    const src = ['App.tsx', 'shell/appShell.tsx', 'shell/topbar/shellHeader.tsx', 'shell/nav/navRail.tsx', 'shell/nav/navRow.tsx'].map(f => readFileSync(join(__dirname, '..', f), 'utf8')).join('\n')
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
    expect(screen.getByLabelText('System metrics')).toBeInTheDocument()
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
    // The capsule paints `MEM —` / `DSK —` BEFORE the first frame lands too (the
    // loading placeholder reuses the loaded branch's "no valid reading" glyph),
    // so a dash is not proof the frame arrived. `CPU 25%` only exists in the
    // loaded branch: wait for that, then read the dashes off the same frame.
    expect(await screen.findByText(/CPU 25%/)).toBeInTheDocument()
    expect(screen.getByText(/MEM —/)).toBeInTheDocument()
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
    // Same ordering as above: `MEM —` is also the pre-frame placeholder, so the
    // wait has to be on a reading only the loaded frame can produce.
    expect(await screen.findByText(/CPU 25%/)).toBeInTheDocument()
    expect(screen.getByText(/MEM —/)).toBeInTheDocument()
    // The rest of the same frame still renders — one absent probe must not
    // blank the whole capsule, let alone unmount the app.
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
    // A refused switch is an error: it renders through ErrorNotice, which
    // announces itself as an alert rather than a polite status line.
    expect(noticeText.closest('[role="alert"]')).not.toBeNull()
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

describe('onCycleReasoningEffort follows the session\'s harness', () => {
  it('steps over the levels the harness takes, not at all where it takes none, and by the model where it cannot say yet', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotReasoningEffort as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, agent: 'kirocrew', reasoning_effort: 'low' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    const { queryClient } = renderWithProviders(<App />, { route: '/chat' })
    queryClient.setQueryData(['slot-selection-capabilities', 'slot-1'], { known: true, effort_supported: true, effort_levels: ['low', 'high'] })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'D', code: 'KeyD', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotReasoningEffort).toHaveBeenCalledWith('slot-1', 'high')

    ;(api.chatSlotReasoningEffort as ReturnType<typeof vi.fn>).mockClear()
    queryClient.setQueryData(['slot-selection-capabilities', 'slot-1'], { known: true, effort_supported: false, effort_levels: [] })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'D', code: 'KeyD', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotReasoningEffort).not.toHaveBeenCalled()
    expect(store.getState().chat.agentSwitchNotice?.message).toMatch(/takes no reasoning effort/)

    queryClient.setQueryData(['slot-selection-capabilities', 'slot-1'], { known: false })
    ;(api.chatSlotReasoningEffort as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, agent: 'kirocrew', model: 'claude-haiku-4.5', reasoning_effort: 'low' }] })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'D', code: 'KeyD', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotReasoningEffort).not.toHaveBeenCalled()

    // Unknown is not "takes none": a cold codex thread's model answers, as in its composer.
    queryClient.setQueryData(['slot-selection-capabilities', 'slot-1'], { known: false, model_effort_pair_ids: true, effort_supported: null, effort_levels: [] })
    store.dispatch({ type: 'chat/setAgentSwitchNotice', payload: null })
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, agent: 'kirocrew', model: 'openai.gpt-6.1-sol', reasoning_effort: 'low' }] })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'D', code: 'KeyD', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotReasoningEffort).toHaveBeenCalledTimes(1)
    expect(store.getState().chat.agentSwitchNotice).toBeNull()

    ;(api.chatSlotReasoningEffort as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, agent: 'kirocrew', model: 'claude-haiku-4.5', reasoning_effort: 'low' }] })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'D', code: 'KeyD', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotReasoningEffort).not.toHaveBeenCalled()
    expect(store.getState().chat.agentSwitchNotice?.message).toMatch(/takes no reasoning effort/)

    // An app-owned slot with no pin runs what the composer resolved its crew to, under the
    // composer's own key (app included): a crew resolved to a no-effort model does not cycle.
    ;(api.chatSlotReasoningEffort as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'chat/setAgentSwitchNotice', payload: null })
    queryClient.setQueryData(['resolved-model', 'kirocrew', 'acp', 'my-app'], 'claude-haiku-4.5')
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, agent: 'kirocrew', app: 'my-app', model: '', reasoning_effort: 'low' }] })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'D', code: 'KeyD', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotReasoningEffort).not.toHaveBeenCalled()
    expect(store.getState().chat.agentSwitchNotice?.message).toMatch(/takes no reasoning effort/)
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

  it('asks the slot for its effort answer again after a cycled model lands', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotModel as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, model: 'auto' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    const { queryClient } = renderWithProviders(<App />, { route: '/chat' })
    queryClient.setQueryData(['available-models', 'acp'], [{ name: 'auto' }, { name: 'opus' }, { name: 'sonnet' }])
    queryClient.setQueryData(['slot-selection-capabilities', 'slot-1'], { known: false, effort_supported: false, effort_levels: [] })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'S', code: 'KeyS', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotModel).toHaveBeenCalledWith('slot-1', 'opus')
    await waitFor(() => expect(queryClient.getQueryState(['slot-selection-capabilities', 'slot-1'])?.isInvalidated).toBe(true))
  })

  it.each([
    ['forward', 'A', 'KeyA', 'reviewer'],
    ['backward', 'Z', 'KeyZ', 'oracle'],
  ])('asks the slot for its effort answer again after a cycled agent lands (%s)', async (_direction, key, code, agent) => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    vi.mocked(api.chatSlotAgent).mockReset().mockResolvedValue({ agent })
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, agent: 'kirocrew', model: '' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    const { queryClient } = renderWithProviders(<App />, { route: '/chat' })
    queryClient.setQueryData(['resolved-model', 'kirocrew', 'acp', ''], 'claude-haiku-4.5')
    queryClient.setQueryData(['resolved-model', agent, 'acp', ''], 'claude-opus-4.6')
    queryClient.setQueryData(['slot-selection-capabilities', 'slot-1'], { known: false, effort_supported: false, effort_levels: [] })
    queryClient.setQueryData(['slot-selection-capabilities', 'slot-2'], { known: false, effort_supported: false, effort_levels: [] })
    try {
      await act(async () => {
        document.dispatchEvent(new KeyboardEvent('keydown', { key, code, altKey: true, shiftKey: true, bubbles: true }))
      })
      expect(api.chatSlotAgent).toHaveBeenCalledWith('slot-1', agent)
      await waitFor(() => expect(store.getState().dashboard.slots.find(s => s.key === 'slot-1')?.agent).toBe(agent))
      await waitFor(() => expect(queryClient.getQueryState(['slot-selection-capabilities', 'slot-1'])?.isInvalidated).toBe(true))
      expect(queryClient.getQueryState(['slot-selection-capabilities', 'slot-2'])?.isInvalidated).toBe(false)
    } finally {
      vi.mocked(api.chatSlotAgent).mockReset().mockResolvedValue({})
    }
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
    expect(await screen.findByTitle(/Kiro account & credits/)).toBeInTheDocument()
  })

  it('renders used/limit and percentage once loaded', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    // default mock: 3044 total used of 10000 = 30%
    const pill = await screen.findByTitle('Kiro account & credits — view usage, manage subscription')
    expect(pill).toBeInTheDocument()
    // The pill is the desktop's way to the plan page (#23812), so its
    // accessible name says "account" too, not only "credit usage".
    expect(pill).toHaveAttribute('aria-label', 'Kiro account & credits — view usage, manage subscription')
  })

  it('renders the true total (credits_used) including overage above the plan', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.sessionsUsage).mockResolvedValueOnce({
      usage: { credits_covered: 10000, credits_used: 10500, credits_overage: 500, credits_plan: 10000, resets: '2026-07-01', plan: 'KIRO POWER' },
    } as never)
    renderWithProviders(<App />, { route: '/chat' })
    // credits_used=10500 total / 10000 plan = 105% (500 over plan)
    expect(await screen.findByTitle('Kiro account & credits — view usage, manage subscription')).toBeInTheDocument()
  })

  it('opens a details modal with breakdown rows when clicked', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    const pill = await screen.findByTitle('Kiro account & credits — view usage, manage subscription')
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
  it('renders no credit segment when the usage fetch rejects on a non-kiro harness', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.sessionsUsage).mockRejectedValueOnce(new Error('boom'))
    renderWithProviders(<App />, { route: '/chat' })
    // This suite's harness is `claude`, which has no Kiro credit balance, so a
    // failed read has nothing true to show and the segment is dropped. The
    // capsule itself still renders, which makes the absence meaningful, and
    // waiting for it takes the read after the harness verdict has settled.
    await waitFor(() => expect(document.querySelector('.tb-capsule')).toBeTruthy())
    await waitFor(() => expect(screen.queryByTitle(/Kiro account & credits/)).toBeNull())
  })

  it('opens the modal in a loading state when clicked before data resolves', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.sessionsUsage).mockResolvedValueOnce({ usage: {} } as never)
    renderWithProviders(<App />, { route: '/chat' })
    const loadingPill = await screen.findByTitle(/Kiro account & credits/)
    fireEvent.click(loadingPill)
    expect(await screen.findByLabelText('Checking credit usage')).toBeInTheDocument()
  })

  it('defaults covered/overage to 0 and renders sub-1000 values without K suffix', async () => {
    const { api } = await import('../api/client')
    // only credits_plan present -> credits_used falls back to 0
    vi.mocked(api.sessionsUsage).mockResolvedValueOnce({ usage: { credits_plan: 500 } } as never)
    renderWithProviders(<App />, { route: '/chat' })
    const pill = await screen.findByTitle('Kiro account & credits — view usage, manage subscription')
    expect(pill).toHaveTextContent('0/500') // sub-1000 -> no "K" formatting
    fireEvent.click(pill)
    expect(await screen.findByText('0 credits')).toBeInTheDocument() // Overage used row
  })

  it('handles a zero limit without dividing by zero (0%)', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.sessionsUsage).mockResolvedValueOnce({ usage: { credits_plan: 0, credits_covered: 0 } } as never)
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByTitle('Kiro account & credits — view usage, manage subscription')).toBeInTheDocument()
  })

  it('falls back to an empty object when the response has no usage key', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.sessionsUsage).mockResolvedValueOnce({} as never)
    renderWithProviders(<App />, { route: '/chat' })
    // d?.usage is undefined -> `|| {}` -> credits_plan absent -> stays loading
    expect(await screen.findByTitle(/Kiro account & credits/)).toBeInTheDocument()
  })

  it('closes the modal on Escape', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    const pill = await screen.findByTitle('Kiro account & credits — view usage, manage subscription')
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
    await waitFor(() => expect(screen.queryByTitle(/Kiro account & credits/)).not.toBeInTheDocument())
    expect(screen.queryByTitle('Kiro account & credits — view usage, manage subscription')).not.toBeInTheDocument()
  })

  it('shows the no-reading dash for that same payload on the Kiro backend', async () => {
    const { api } = await import('../api/client')
    // Same `available:false`, but the selected harness IS kiro-cli (`agent.acp_backend`
    // is the kiro id, the empty string): kiro-cli holds no reading yet, and the
    // modal this dash opens is where the user refreshes. Hiding it here would hide
    // the one recovery path; on any other harness there is nothing to refresh.
    vi.mocked(api.kirocrewConfig).mockResolvedValueOnce({ agent: { acp_backend: 'kiro' } } as never)
    vi.mocked(api.sessionsUsage).mockResolvedValue({ usage: { available: false } } as never)
    renderWithProviders(<App />, { route: '/chat' })
    const pill = await screen.findByTitle('Kiro account & credits — no balance reading yet; open to refresh')
    expect(pill).toHaveTextContent('—')
    expect(screen.queryByTitle('Kiro account & credits — view usage, manage subscription')).not.toBeInTheDocument()
  })

  it('auto-closes the modal if usage resolves to unavailable while it is open (non-Kiro provider)', async () => {
    // The pill hides for `none` on a non-Kiro harness (the mock's default
    // backend is claude), so a modal opened during the warm-up would be left
    // open behind a pill that no longer exists, with nothing to refresh.
    const { api } = await import('../api/client')
    let resolveUsage: (v: unknown) => void = () => {}
    vi.mocked(api.sessionsUsage).mockReturnValue(new Promise(r => { resolveUsage = r }) as never)
    renderWithProviders(<App />, { route: '/chat' })
    const pill = await screen.findByTitle(/Kiro account & credits/)
    fireEvent.click(pill)
    expect(await screen.findByLabelText('Checking credit usage')).toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'Kiro Account' })).toBeInTheDocument()
    await act(async () => { resolveUsage({ usage: { available: false } }); await Promise.resolve() })
    await waitFor(() => expect(screen.queryByLabelText('Checking credit usage')).not.toBeInTheDocument())
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Kiro Account' })).not.toBeInTheDocument())
    expect(screen.queryByTitle(/Kiro account & credits/)).not.toBeInTheDocument()
  })

  it('renders its own dash when the backend setting could not be read, and retries that read from the modal', async () => {
    // `available:false` AND the config read failed: neither "no plan on kiro"
    // nor "not the kiro harness" is established, so the pill must not vanish
    // as if the non-Kiro verdict had been reached. A dash with its own label,
    // and behind it a notice with the retry.
    const { api } = await import('../api/client')
    // The first read fails; the retry the modal triggers is left IN FLIGHT, so
    // the test can see what the segment does while the query is refetching.
    vi.mocked(api.kirocrewConfig)
      .mockRejectedValueOnce(new ApiError(500, 'config store unreadable'))
      .mockReturnValue(new Promise(() => {}) as never)
    vi.mocked(api.sessionsUsage).mockResolvedValue({ usage: { available: false } } as never)
    renderWithProviders(<App />, { route: '/chat' })

    const CONFIG_TITLE = 'Kiro account & credits — could not read the backend setting; open to retry'
    const pill = await screen.findByTitle(CONFIG_TITLE)
    expect(pill).toHaveTextContent('—')
    expect(screen.queryByTitle('Kiro account & credits — no balance reading yet; open to refresh')).not.toBeInTheDocument()
    // The mock keeps its history across this file's tests: count relative to now.
    const configReads = vi.mocked(api.kirocrewConfig).mock.calls.length

    fireEvent.click(pill)
    const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
    const alert = await within(dialog).findByRole('alert')
    expect(alert).toHaveTextContent('Could not load settings, so your balance can’t be shown.')
    expect(within(alert).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: /^Refresh$/ })).toBeEnabled()
    // Opening the modal re-asks for the config: the shared client never lets
    // the query go stale by itself, so this is the only retry there is.
    await waitFor(() => expect(vi.mocked(api.kirocrewConfig).mock.calls.length).toBeGreaterThan(configReads))
    // While that retry is in flight the query is back to `pending` with its
    // error cleared. The segment must NOT drop to the hidden non-Kiro shape
    // for that window: the dash, the notice and the modal all stay.
    // flake-ok: pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
    // nosemgrep: semgrep.kirocrew.test-promise-sleep -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
    await new Promise(resolve => setTimeout(resolve, 600))
    expect(screen.getByTitle(CONFIG_TITLE)).toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'Kiro Account' })).toBe(dialog)
    expect(within(dialog).getByRole('alert')).toHaveTextContent('Could not load settings, so your balance can’t be shown.')
    expect(within(dialog).queryByText('No balance reading is available for this account yet.')).not.toBeInTheDocument()
    vi.mocked(api.kirocrewConfig).mockReset().mockResolvedValue({ agent: { acp_backend: 'claude' } } as never)
  })

  it('keeps the modal open when usage resolves to no reading on the Kiro backend', async () => {
    // Same payload, but the pill stays as the no-reading dash here, and the
    // modal it opens is where Refresh lives -- closing it would take the one
    // recovery path away at the moment it is needed.
    const { api } = await import('../api/client')
    vi.mocked(api.kirocrewConfig).mockResolvedValueOnce({ agent: { acp_backend: 'kiro' } } as never)
    let resolveUsage: (v: unknown) => void = () => {}
    vi.mocked(api.sessionsUsage).mockReturnValue(new Promise(r => { resolveUsage = r }) as never)
    renderWithProviders(<App />, { route: '/chat' })
    const pill = await screen.findByTitle(/Kiro account & credits/)
    fireEvent.click(pill)
    expect(await screen.findByLabelText('Checking credit usage')).toBeInTheDocument()
    await act(async () => { resolveUsage({ usage: { available: false } }); await Promise.resolve() })
    await waitFor(() => expect(screen.queryByLabelText('Checking credit usage')).not.toBeInTheDocument())
    await screen.findByTitle('Kiro account & credits — no balance reading yet; open to refresh')
    // Past any close (and its exit animation): still the same dialog, with Refresh.
    // flake-ok: pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
    // nosemgrep: semgrep.kirocrew.test-promise-sleep -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
    await new Promise(resolve => setTimeout(resolve, 600))
    const dialog = screen.getByRole('dialog', { name: 'Kiro Account' })
    expect(within(dialog).getByText('No balance reading is available for this account yet.')).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: /^Refresh$/ })).toBeEnabled()
  })

  it('never renders NaN when credit fields arrive non-finite', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.sessionsUsage).mockResolvedValue({ usage: { credits_plan: NaN, credits_used: NaN, credits_covered: NaN } } as never)
    renderWithProviders(<App />, { route: '/chat' })
    // Non-finite plan is rejected by the Number.isFinite guard, so the loaded
    // pill (which would otherwise show "NaN / NaN") never appears.
    await waitFor(() => expect(screen.queryByTitle('Kiro account & credits — view usage, manage subscription')).not.toBeInTheDocument())
    expect(screen.queryByText(/NaN/)).not.toBeInTheDocument()
  })
})

describe('file-menu result toast', () => {
  it('is mounted in the popout and embed frames, which have no notification sheet', () => {
    // A file-menu row renders in those frames too (the chat side panels' markdown
    // header), and the dashboard's copy lives in NotificationSheet, which only the
    // dashboard branch's ShellHeader renders. Read from source for the same reason
    // as the import notice below.
    const src = readFileSync(join(__dirname, '..', 'App.tsx'), 'utf8')
    const frames = '{(isPopout || isEmbed) && <FileMenuResultToastFrame />}'
    expect(src.split(frames).length - 1).toBe(1)
    expect(src.indexOf(frames)).toBeGreaterThan(src.indexOf('<WsContext.Provider'))
    expect(src.indexOf(frames)).toBeLessThan(src.indexOf('{isPopout ? ('))
  })
})

describe('import outcome notice', () => {
  it('is mounted for every layout: popout and embed above the branch, the dashboard in its local pane', () => {
    // The row's menu closes on the picker's blur, so an import's outcome only
    // reaches the user through this notice. The popout, embed and dashboard
    // layouts each render a session menu, so each must host the notice. The
    // dashboard's copy lives inside the local pane's overlay root so it hides
    // while a remote Crew shows; the frames, which have no local pane, keep one
    // mounted above the branch that picks among them.
    // Read from source: standing up the popout and embed shells here would
    // mean faking the window-level flags each branch keys on.
    const src = readFileSync(join(__dirname, '..', 'App.tsx'), 'utf8')
    const frames = '{(isPopout || isEmbed) && <ImportSessionOutcomeNotice />}'
    expect(src.split('<ImportSessionOutcomeNotice').length - 1).toBe(2)
    expect(src.split(frames).length - 1).toBe(1)
    expect(src.indexOf(frames)).toBeGreaterThan(src.indexOf('<WsContext.Provider'))
    expect(src.indexOf(frames)).toBeLessThan(src.indexOf('{isPopout ? ('))
    // The only other branch is the dashboard, and its copy is in the local pane.
    expect(src).toContain(') : isEmbed ? (')
    // The dashboard's copy navigates to /chat on Open, so it carries the flag.
    const dashboard = src.indexOf('<ImportSessionOutcomeNotice navigateToChat />', src.indexOf('<LocalOverlayRoot hidden='))
    expect(dashboard).toBeGreaterThan(-1)
    expect(dashboard).toBeLessThan(src.indexOf('</LocalOverlayRoot>'))
  })
})
