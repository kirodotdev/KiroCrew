/**
 * Test: the top-bar Kiro credit segment separates "still loading" from "failed".
 *
 * The segment reads three things off one query: a business object, `null` (the
 * gateway's usage cache has not warmed), and `'none'` (the gateway holds no
 * reading, shown as a dash that opens the modal to refresh). None of those covers a FAILED request, whose
 * `data` is `undefined` — and `undefined` is falsy exactly like `null`, so a
 * 503 from `/api/sessions/usage` used to render the warming spinner forever
 * while the 30s refetch retried behind it. The fix reads `isError` alongside
 * `data`; these tests pin both branches so they cannot collapse back together.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { i18nT } from '../i18n/t'
import type { RootState } from '../store'
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

const { sessionsUsageMock, isMobileMock } = vi.hoisted(() => ({
  sessionsUsageMock: vi.fn(),
  isMobileMock: vi.fn(() => false),
}))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => isMobileMock() }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    sessionsUsage: sessionsUsageMock,
    // The kiro-cli harness: the no-reading dash below is a Kiro-backend surface.
    kirocrewConfig: vi.fn().mockResolvedValue({ agent: { acp_backend: '' } }),
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

const connectedState = {
  dashboard: { connected: true, status: { platform: 'darwin' }, slots: [], approvalMode: 'normal' } as unknown as RootState['dashboard'],
}

describe('top-bar credit segment — failed vs loading', () => {
  beforeEach(() => {
    sessionsUsageMock.mockReset()
    isMobileMock.mockReturnValue(false)
  })

  it('renders the unavailable label when the usage fetch fails', async () => {
    // A 503 from the gateway's readiness gate is the real-world shape of this.
    sessionsUsageMock.mockRejectedValue(Object.assign(new Error('Service Unavailable'), { status: 503 }))
    renderWithProviders(<App />, { route: '/chat', preloadedState: connectedState })

    expect(await screen.findByLabelText(i18nT('app.kiro_credit_usage_unavailable'))).toBeTruthy()
    // The warming spinner must be gone — that is the defect being pinned.
    expect(screen.queryByLabelText(i18nT('app.kiro_credit_usage_checking_2'))).toBeNull()
  })

  it('keeps the warming spinner while the request is still in flight', async () => {
    // Never settles, so the query stays pending and never reaches isError.
    sessionsUsageMock.mockReturnValue(new Promise(() => {}))
    renderWithProviders(<App />, { route: '/chat', preloadedState: connectedState })

    expect(await screen.findByLabelText(i18nT('app.kiro_credit_usage_checking_2'))).toBeTruthy()
    expect(screen.queryByLabelText(i18nT('app.kiro_credit_usage_unavailable'))).toBeNull()
  })

  it('shows the reading once usage resolves', async () => {
    sessionsUsageMock.mockResolvedValue({
      usage: { credits_used: 3044, credits_plan: 10000, resets: '2026-09-01', plan: 'KIRO POWER' },
    })
    renderWithProviders(<App />, { route: '/chat', preloadedState: connectedState })

    expect(await screen.findByLabelText(i18nT('components.kiroAccountModal.kiro_credit_usage'))).toBeTruthy()
    expect(screen.queryByLabelText(i18nT('app.kiro_credit_usage_unavailable'))).toBeNull()
    expect(screen.queryByLabelText(i18nT('app.kiro_credit_usage_checking_2'))).toBeNull()
  })

  it('keeps the dash on mobile, where the reading and the spinner are dropped', async () => {
    // On a narrow viewport the segment renders neither the numbers nor the
    // spinner, so without the dash the failed and warming states differ only by
    // an opacity class on the same coin glyph — not a distinction a user can see.
    isMobileMock.mockReturnValue(true)
    sessionsUsageMock.mockRejectedValue(Object.assign(new Error('Service Unavailable'), { status: 503 }))
    renderWithProviders(<App />, { route: '/chat', preloadedState: connectedState })

    const failed = await screen.findByLabelText(i18nT('app.kiro_credit_usage_unavailable'))
    expect(failed.textContent).toContain('—')
  })

  it('treats an api_key_auth unavailable payload as terminal, not still loading', async () => {
    // The backend fail-fasts API-key accounts with a reasoned marker (#5728).
    // That payload must stop the spinner and say WHY — before the fix the
    // panel spun forever because no terminal state ever arrived.
    sessionsUsageMock.mockResolvedValue({ usage: { available: false, reason: 'api_key_auth' } })
    renderWithProviders(<App />, { route: '/chat', preloadedState: connectedState })

    const pill = await screen.findByLabelText(i18nT('app.kiro_credit_usage_api_key'))
    expect(pill.textContent).toContain('—')
    expect(screen.queryByLabelText(i18nT('app.kiro_credit_usage_checking_2'))).toBeNull()
    expect(screen.queryByLabelText(i18nT('app.kiro_credit_usage_unavailable'))).toBeNull()
  })

  it('keeps the pill as a dash on a reasonless unavailable payload, naming where to refresh', async () => {
    // The gateway holds no reading (plan-less account with the scrape parked or
    // failed). The modal this dash opens carries the Refresh that can fix that,
    // so the dash must stay on screen -- a hidden pill would make that path
    // unreachable. Distinct from the api_key_auth dash: the label differs.
    sessionsUsageMock.mockResolvedValue({ usage: { available: false } })
    renderWithProviders(<App />, { route: '/chat', preloadedState: connectedState })

    const pill = await screen.findByLabelText(i18nT('app.kiro_credit_usage_no_reading'))
    expect(pill.textContent).toContain('—')
    expect(screen.queryByLabelText(i18nT('app.kiro_credit_usage_checking_2'))).toBeNull()
    expect(screen.queryByLabelText(i18nT('app.kiro_credit_usage_api_key'))).toBeNull()
    expect(screen.queryByLabelText(i18nT('app.kiro_credit_usage_unavailable'))).toBeNull()
  })

  it('keeps the account modal open with Refresh when usage resolves to no reading', async () => {
    // Opened while the cache was warming, then the reading resolves to `none`.
    // The modal is where the user refreshes, so it must stay open with the
    // primary Refresh under the no-reading notice -- closing it would take the
    // recovery path away at the exact moment it is needed.
    sessionsUsageMock.mockResolvedValueOnce({ usage: {} })
    sessionsUsageMock.mockResolvedValue({ usage: { available: false } })
    const { queryClient } = renderWithProviders(<App />, { route: '/chat', preloadedState: connectedState })

    fireEvent.click(await screen.findByLabelText(i18nT('app.kiro_credit_usage_checking_2')))
    const dialog = await screen.findByRole('dialog', { name: i18nT('components.kiroAccountModal.kiro_account') })

    await queryClient.invalidateQueries({ queryKey: ['kiro-usage'] })
    await waitFor(() => expect(queryClient.getQueryData(['kiro-usage'])).toBe('none'))

    // The modal re-renders with the no-reading notice and the primary Refresh...
    await screen.findByText(i18nT('components.kiroAccountModal.credit_usage_no_reading'))
    expect(screen.getByRole('button', { name: i18nT('components.kiroAccountModal.refresh_balance') })).toBeEnabled()
    // ...and is still there once any close (and its exit animation) would have
    // landed. An auto-close on `none` removes the dialog well inside this window.
    await new Promise(resolve => setTimeout(resolve, 600))
    expect(screen.queryByRole('dialog', { name: i18nT('components.kiroAccountModal.kiro_account') })).toBe(dialog)
    expect(screen.getByRole('button', { name: i18nT('components.kiroAccountModal.refresh_balance') })).toBeEnabled()
  })
})
