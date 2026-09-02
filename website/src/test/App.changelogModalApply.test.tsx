/**
 * Test: the changelog modal must not offer an apply the gateway will refuse.
 *
 * This is the user-visible half of the reported bug. `POST /api/update` is git
 * fetch + reset, so it only ever works on a checkout — a wheel install answers
 * 400/409 and a desktop bundle is owned by its own updater. The modal used to
 * key its "Update now" button on availability ALONE, so on the install shape most
 * users run, the one button the update flow put in front of them was guaranteed to
 * fail. Availability and capability are separate facts, and `updateAffordance` is
 * the single place that combines them.
 *
 * The modal is mounted through the real App shell rather than in isolation
 * because the affordance is wired from redux status there; the surrounding pages
 * are stubbed the same way the other App.* tests stub them.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import type { RootState } from '../store'
import App from '../App'

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/AgentsPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))

// `vi.mock` is hoisted above the module body, so a plain const declared here is
// still uninitialised when the factory runs. `vi.hoisted` is the seam for a value
// both the factory and the assertions need.
//
// `statusOverride` is mutable on purpose: the /api/status fetch lands AFTER mount
// and writes the same slice the preloaded state seeds, so a fixed fetch payload
// silently clobbers whatever a test set up and every case would test one shape.
const { COMMAND, statusOverride } = vi.hoisted(() => ({
  COMMAND: 'python3 -m pip install --upgrade kiro-crew',
  statusOverride: { value: {} as Record<string, unknown> },
}))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    // The status FETCH must agree with the preloaded state: it lands after mount
    // and writes the same slice, so a fixed payload here would overwrite the
    // capability each test is trying to exercise.
    status: vi.fn().mockImplementation(async () => ({
      uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
      version: '0.2.0rc9', update_available: true, update_can_apply: false,
      update_check_status: 'succeeded', update_command: COMMAND,
      ...statusOverride.value,
    })),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { credits_used: 0, credits_covered: 0, credits_plan: 0, resets: '2026-07-01', plan: 'KIRO POWER', cost_usd: 0, overage_rate: '0.04' } }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
    changelog: vi.fn().mockResolvedValue({ content: '## [0.2.0rc9]\n- a new entry\n' }),
    setAutoUpdate: vi.fn().mockResolvedValue({}),
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

/** A wheel install with an update waiting that it cannot apply itself. */
const wheelState = (over: Record<string, unknown> = {}) => {
  // One shape drives BOTH the preloaded store and the /api/status reply, so the
  // fetch that lands after mount confirms the case instead of overwriting it.
  statusOverride.value = over
  return {
    dashboard: {
      connected: true,
      slots: [],
      approvalMode: 'normal',
      status: {
        platform: 'linux',
        version: '0.2.0rc9',
        update_available: true,
        update_can_apply: false,
        update_check_status: 'succeeded',
        update_command: COMMAND,
        ...over,
      },
    } as unknown as RootState['dashboard'],
  }
}

describe('changelog modal apply affordance', () => {
  beforeEach(() => {
    // A DIFFERENT last-seen version is what opens the modal on mount.
    localStorage.setItem('mc-last-version', '0.2.0rc8')
    statusOverride.value = {}
  })

  it('offers the command, not a button the gateway would refuse, on a wheel install', async () => {
    renderWithProviders(<App />, { route: '/chat', preloadedState: wheelState() })

    expect(await screen.findByTestId('modal-update-command')).toBeTruthy()
    // The exact command is whatever the gateway composed for this install shape;
    // the fixture only has to be recognisable here.
    expect(screen.getByTestId('modal-update-command').textContent).toContain('kiro-crew')
    // The regression guard: this button is a guaranteed 400/409 here.
    expect(screen.queryByText('Update Now')).toBeNull()
  })

  it('still offers the in-app apply on a checkout, which the gateway can act on', async () => {
    renderWithProviders(<App />, {
      route: '/chat',
      preloadedState: wheelState({ update_can_apply: true }),
    })

    expect(await screen.findByText('Update Now')).toBeTruthy()
    expect(screen.queryByTestId('modal-update-command')).toBeNull()
  })

  it('offers nothing to click when there is no verdict yet', async () => {
    // `null` is "no answer", not "no update" — a check that never completed must
    // not be rendered as an available update with an action attached.
    renderWithProviders(<App />, {
      route: '/chat',
      preloadedState: wheelState({ update_available: null, update_check_status: 'failed' }),
    })

    await waitFor(() => expect(screen.getByTestId('chat-page')).toBeTruthy())
    expect(screen.queryByText('Update Now')).toBeNull()
    expect(screen.queryByTestId('modal-update-command')).toBeNull()
  })
})
