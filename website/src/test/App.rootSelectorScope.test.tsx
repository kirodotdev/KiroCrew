/**
 * Test: the app root's `dashboard` subscription is no wider than the fields it uses.
 *
 * `useAppSelector` is plain `useSelector` with reference equality, and the `dashboard`
 * slice carries much more than the two fields the root reads (the slot list, the subagent
 * maps), so under Immer a reducer touching any of them re-renders the root for nothing.
 *
 * These count renders of `App` ITSELF: `useTerminalPoppedOut` is called exactly once,
 * unconditionally, in the root's body and nowhere else, so wrapping it is an exact
 * per-render counter. A `<Profiler>` counts subtree commits instead, which a child that
 * legitimately subscribes would be indistinguishable from.
 *
 * A slots frame is asserted too, and is the high-frequency case: `sseSlots` assigns a new
 * array on every websocket frame, so a reference-equality subscription re-renders forever.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import type { ChatSlot } from '../types'
import App from '../App'
import {
  sseConnected,
  sseSlots,
  sseDisconnected,
  setUpdateProgress,
  setChannelTrusted,
  sseSubagentStatus,
} from '../store/dashboardSlice'

// Counts renders of `App`. Declared via vi.hoisted so the mock factory below can
// reference it regardless of hoisting order.
const { appRenders } = vi.hoisted(() => ({ appRenders: { n: 0 } }))

vi.mock('../utils/terminalPopout', async importOriginal => {
  const actual = await importOriginal<typeof import('../utils/terminalPopout')>()
  return {
    ...actual,
    // Delegates to the real hook so hook order and behaviour are untouched.
    useTerminalPoppedOut: () => {
      appRenders.n++
      return actual.useTerminalPoppedOut()
    },
  }
})

// The root is heavy and the routes under it are irrelevant to selector scope.
vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/AgentsPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/CapabilitiesPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))
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
    themeBoot: vi.fn().mockResolvedValue({ mode: '', color: '', onboarded: true, import_onboarded: true }),
    updateThemeConfig: vi.fn().mockResolvedValue({}),
    onboardingImportScan: vi.fn().mockResolvedValue({ sources: [], skipped: [], merge_only: true }),
    onboardingImportState: vi.fn().mockResolvedValue({}),
    beaconStatus: vi.fn().mockResolvedValue({ enabled: true, would_send: true, reason: 'ready', endpoint_configured: true, env_override: false, env_var: 'KIROCREW_TELEMETRY_DISABLED' }),
    patchConfig: vi.fn().mockResolvedValue({}),
  },
  isAuthBannerShown: vi.fn(() => false),
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

const slot = (key: string, messages: number): ChatSlot => ({ key, messages, running: false })

/**
 * Mounts the true root against a real store and settles the mount-time async work
 * (status/usage/app queries) so later render counts measure only the dispatch under
 * test. Settling waits for the render count to actually stop moving rather than for a
 * fixed delay, because a fixed delay lets residual mount work land inside the
 * measurement window and read as a dispatch-caused render. Returns the store so tests
 * can dispatch real slice actions.
 */
async function mountSettled() {
  const rendered = renderWithProviders(<App />, { route: '/chat' })
  await waitFor(() => expect(screen.getByTestId('chat-page')).toBeTruthy())
  await act(async () => {
    rendered.store.dispatch(sseConnected())
    rendered.store.dispatch(sseSlots([slot('chat-1-1', 1)]))
    await new Promise(r => setTimeout(r, 200))
  })
  // One late render lands ~500ms after mount, so a short quiet window is not enough.
  // The no-dispatch control in the first case is what proves this actually settled.
  let quiet = 0
  for (let i = 0; i < 60 && quiet < 5; i++) {
    const mark = appRenders.n
    await act(async () => { await new Promise(r => setTimeout(r, 100)) })
    quiet = appRenders.n === mark ? quiet + 1 : 0
  }
  return rendered
}

/** Runs `body`, lets React flush, and returns how many times the root rendered. */
async function rendersDuring(body: () => void) {
  const before = appRenders.n
  await act(async () => {
    body()
    await new Promise(r => setTimeout(r, 150))
  })
  return appRenders.n - before
}

describe('App root store subscription scope', () => {
  beforeEach(() => { appRenders.n = 0 })
  afterEach(() => { cleanup(); vi.clearAllMocks() })

  it('does not re-render the root on a subagent-status frame', async () => {
    const { store } = await mountSettled()

    // Nothing at all happens in the window, so a non-zero count below would be
    // background async work rather than the dispatch. Measured 0.
    expect(await rendersDuring(() => {})).toBe(0)

    // Live websocket traffic: writes only the subagent maps, which the root never
    // reads. The slice object identity still changes, which is what used to be enough.
    const delta = await rendersDuring(() => {
      store.dispatch(sseSubagentStatus({ running: 2, slot: 'chat-1-1' }))
    })

    expect(store.getState().dashboard.subagentRunning['chat-1-1']).toBe(2)
    expect(delta).toBe(0)
    // The root did not lose the values by simply dropping the subscription.
    expect(store.getState().dashboard.connected).toBe(true)
    expect(store.getState().dashboard.updateProgress).toBeNull()
    expect(screen.queryByLabelText('Gateway offline')).toBeNull()
  })

  it('does not re-render the root when channel trust changes', async () => {
    const { store } = await mountSettled()

    const delta = await rendersDuring(() => {
      store.dispatch(setChannelTrusted(true))
    })

    expect(store.getState().dashboard.channelTrusted).toBe(true)
    expect(delta).toBe(0)
  })

  it('still re-renders the root when the connection flag changes', async () => {
    const { store } = await mountSettled()

    const delta = await rendersDuring(() => {
      store.dispatch(sseDisconnected())
    })

    expect(delta).toBeGreaterThan(0)
    // The offline signal is on screen, so the narrowed selector is genuinely wired.
    expect(screen.getByLabelText('Gateway offline')).toBeTruthy()
  })

  it('still re-renders the root when the update progress changes', async () => {
    const { store } = await mountSettled()

    // `updateProgress` has no direct DOM output in the root (it gates an effect), so
    // the render count is the only observable that this second selector is live.
    const delta = await rendersDuring(() => {
      store.dispatch(setUpdateProgress({ step: 'download', detail: 'fetching' }))
    })

    expect(delta).toBeGreaterThan(0)
    expect(store.getState().dashboard.updateProgress).toEqual({ step: 'download', detail: 'fetching' })
  })

  it('does not re-render the root on a slots frame', async () => {
    const { store } = await mountSettled()

    // Control: nothing is dispatched, so a non-zero count here is background work.
    expect(await rendersDuring(() => {})).toBe(0)

    // `sseSlots` assigns a new array and rebuilds `slotHistory`, so both go reference-unequal.
    const delta = await rendersDuring(() => {
      store.dispatch(sseSlots([slot('chat-1-1', 2), slot('chat-1-2', 0)]))
    })

    expect(store.getState().dashboard.slots).toHaveLength(2)
    expect(delta).toBe(0)
  })
})
