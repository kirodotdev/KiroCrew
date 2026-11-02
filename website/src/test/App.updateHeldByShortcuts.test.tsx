/**
 * Test: an update prompt waits while the keyboard shortcuts dialog is open.
 *
 * Both dialogs trap Tab on a window-capture listener, so two of them open at
 * once would each pull focus back to themselves on every Tab. App holds the
 * "update ready" prompt while the shortcuts dialog is up, and shows it once that
 * dialog closes.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { i18nT } from '../i18n/t'
import App from '../App'

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

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({
      uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
      platform: 'linux', version: '0.9.0',
      update_available: false, update_can_apply: false, update_check_status: 'succeeded',
    }),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { credits_used: 0, credits_covered: 0, credits_plan: 0, resets: '2026-07-01', plan: 'KIRO POWER', cost_usd: 0, overage_rate: '0.04' } }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
    changelog: vi.fn().mockResolvedValue({ content: '' }),
    setAutoUpdate: vi.fn().mockResolvedValue({}),
    kirocrewConfig: vi.fn().mockResolvedValue({ auto_update: true }),
    themeBoot: vi.fn().mockResolvedValue({ onboarded: true, import_onboarded: true, privacy_acked: true }),
    dashboardConfig: vi.fn().mockResolvedValue({ social_share_enabled: false }),
    featureVideoNext: vi.fn().mockResolvedValue({ video: null, enabled: false, download_enabled: false }),
    featureVideoFeedback: vi.fn().mockResolvedValue({ ok: true }),
    featureVideoProbe: vi.fn().mockResolvedValue({ ok: true }),
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

/** Upper bound on the boot chain and the cache notification. */
const WAIT_MS = 5000

const shortcutsName = () => i18nT('components.shortcutsModal.keyboard_shortcuts')
const updateReady = () => screen.queryByRole('dialog', { name: i18nT('components.updateModal.update_ready') })

beforeEach(() => {
  localStorage.clear()
  vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 200 })))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('App holds the update prompt behind the shortcuts dialog', () => {
  it('shows the update prompt only once the shortcuts dialog closes', async () => {
    const { queryClient } = renderWithProviders(<App />)
    await waitFor(() => expect(screen.getByTestId('chat-page')).toBeTruthy(), { timeout: WAIT_MS })

    fireEvent.keyDown(document.body, { key: '/', code: 'Slash', ctrlKey: true })
    await screen.findByRole('dialog', { name: shortcutsName() }, { timeout: WAIT_MS })

    await act(async () => { queryClient.setQueryData(['update-state'], { state: 'downloaded', version: '9.9.9' }) })
    expect(updateReady()).toBeNull()

    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: shortcutsName() })).toBeNull(), { timeout: WAIT_MS })
    await waitFor(() => expect(updateReady()).not.toBeNull(), { timeout: WAIT_MS })
  })
})
