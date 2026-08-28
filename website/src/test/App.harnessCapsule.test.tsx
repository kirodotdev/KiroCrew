/**
 * Top-bar capsule: the harness readout, and the credit segment it gates.
 *
 * The bug being pinned: the only signal that hid the credit pill was
 * `usage.available === false`, which the gateway sets when kiro-cli is ABSENT
 * FROM THE HOST. That is a host-presence test, not a harness test — so with
 * kiro-cli installed and a registry adapter selected, the pill kept rendering a
 * Kiro balance that no turn was drawing down. `status.harness.kiro_credits` is
 * the gateway's own answer, and this file asserts the header obeys it including
 * during the window where the usage cache still holds the old reading.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'

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

// `vi.mock` factories are hoisted above these declarations, so the fixture and
// the status spy have to be created in a hoisted block to be reachable from one.
const { USAGE, statusMock } = vi.hoisted(() => ({
  // A real credit reading on every run: the pill MUST be hidden by the harness
  // answer alone, not because there was nothing to show.
  USAGE: { usage: { credits_used: 117000, credits_plan: 10000, plan: 'KIRO POWER' } },
  statusMock: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: () => statusMock(),
    sessionsUsage: vi.fn().mockResolvedValue(USAGE),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
  },
  isAuthBannerShown: vi.fn(() => false),
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) { super(message); this.status = status }
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
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }

import App from '../App'
import { PREVIEW_ACP_BACKENDS } from '../utils/previewFlags'

const BASE_STATUS = { uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }

const setHarness = (harness: unknown) => {
  statusMock.mockResolvedValue(harness === undefined ? BASE_STATUS : { ...BASE_STATUS, harness })
}

describe('App top bar — harness readout', () => {
  beforeEach(() => {
    localStorage.clear()
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 1400 })
    statusMock.mockReset()
  })

  it('names the harness the gateway reported', async () => {
    setHarness({ backend: 'codex', label: 'OpenAI Codex', kiro_credits: false })
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByLabelText(/New sessions use OpenAI Codex/)).toBeTruthy()
  })

  it('hides the credit segment for a harness that bills its own vendor account', async () => {
    setHarness({ backend: 'codex', label: 'OpenAI Codex', kiro_credits: false })
    renderWithProviders(<App />, { route: '/chat' })
    // Wait on the harness segment so the status query has certainly resolved —
    // asserting the pill's absence before that would pass for the wrong reason.
    await screen.findByLabelText(/New sessions use OpenAI Codex/)
    expect(screen.queryByLabelText('Kiro credit usage')).toBeNull()
  })

  it('keeps the credit segment on the first-class Kiro harness', async () => {
    // ACP_BACKEND_KIRO is the empty string. Hiding its constant harness label
    // must not take the independently useful credit segment with it.
    setHarness({ backend: '', label: 'Kiro CLI', kiro_credits: true })
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByLabelText('Kiro credit usage')).toBeTruthy()
    expect(screen.queryByLabelText(/New sessions use Kiro CLI/)).toBeNull()
  })

  it('behaves as before when the gateway sends no harness block', async () => {
    // An older gateway: UNKNOWN must not hide a balance the operator is really
    // spending, and must not invent a harness label either.
    setHarness(undefined)
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByLabelText('Kiro credit usage')).toBeTruthy()
    expect(screen.queryByLabelText(/New sessions use /)).toBeNull()
  })

  it('keeps the readout passive when the preview is enabled', async () => {
    localStorage.setItem(PREVIEW_ACP_BACKENDS, '1')
    setHarness({ backend: 'codex', label: 'OpenAI Codex', kiro_credits: false })
    renderWithProviders(<App />, { route: '/chat' })
    const readout = await screen.findByLabelText(/New sessions use OpenAI Codex/)
    expect(readout.tagName).toBe('SPAN')
    expect(readout.textContent).toMatch(/New sessions/)
  })

  it('hides the constant default readout while the preview is disabled', async () => {
    setHarness({ backend: '', label: 'Kiro CLI', kiro_credits: true })
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByLabelText('Kiro credit usage')).toBeTruthy()
    expect(screen.queryByLabelText(/New sessions use Kiro CLI/)).toBeNull()
  })

  it('keeps a non-default readout visible when its preview was later disabled', async () => {
    setHarness({ backend: 'codex', label: 'OpenAI Codex', kiro_credits: false })
    renderWithProviders(<App />, { route: '/chat' })
    const readout = await screen.findByLabelText(/New sessions use OpenAI Codex/)
    expect(readout.tagName).not.toBe('BUTTON')
  })

  it('folds the harness readout away with the rest of the capsule', async () => {
    setHarness({ backend: 'claude', label: 'Claude Code', kiro_credits: false })
    localStorage.setItem('mc-topbar-capsule-collapsed', '1')
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('chat-page')
    expect(screen.queryByLabelText(/New sessions use Claude Code/)).toBeNull()
  })
})
