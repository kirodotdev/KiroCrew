/**
 * Test: App top-bar offline pill suppression when auth banner is shown.
 *
 * When the gateway returns 403 + X-Auth-Required, api/client.ts injects
 * the red "Session expired — paste kirocrew token" banner at the top of
 * the page AND fires a `mc-auth-required` window event. Without
 * coordination, App's pulsing "Offline" pill in the top-bar would render
 * alongside that banner — two banners arguing about the same root cause,
 * and the louder of the two (the pulsing pill) is the less actionable.
 *
 * App listens for `mc-auth-required` / `mc-auth-cleared` and toggles a
 * local `authRequired` flag. When true, the offline pill is replaced
 * with a screen-reader-only marker — auth banner is the canonical signal.
 * On mount, it seeds the flag from `isAuthBannerShown()` to handle the
 * case where the 403 fired before App hydrated.
 *
 * These tests pin three contracts:
 *   1. WS disconnected + no auth banner → pulsing pill is rendered.
 *   2. WS disconnected + auth banner shown → pill is suppressed; only the
 *      sr-only marker remains.
 *   3. `mc-auth-required` event mid-session → pill suppression flips on
 *      live (no remount required).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import type { RootState } from '../store'
import App from '../App'

// Match the App.test.tsx mock setup. Differ only in `isAuthBannerShown`
// where each test controls it explicitly.
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

const { isAuthBannerShownMock } = vi.hoisted(() => ({
  isAuthBannerShownMock: vi.fn<[], boolean>(() => false),
}))
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
  },
  isAuthBannerShown: isAuthBannerShownMock,
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

describe('App offline pill — auth banner suppression', () => {
  beforeEach(() => {
    isAuthBannerShownMock.mockReset()
    isAuthBannerShownMock.mockReturnValue(false)
  })

  it('shows the pulsing "Offline" pill when WS is disconnected AND no auth banner', () => {
    renderWithProviders(<App />, {
      route: '/chat',
      preloadedState: {
        dashboard: { connected: false, status: { platform: 'darwin' }, slots: [], approvalMode: 'normal' } as unknown as RootState['dashboard'],
      },
    })
    // The pill carries `aria-label="Gateway offline"`. The sr-only fallback
    // is only present when authRequired is true; assert it's NOT here.
    expect(screen.getByLabelText('Gateway offline')).toBeTruthy()
    expect(screen.queryByText(/session expired, see banner above/i)).toBeNull()
  })

  it('suppresses the pill and renders only a sr-only marker when auth banner is shown on mount', () => {
    isAuthBannerShownMock.mockReturnValue(true)
    renderWithProviders(<App />, {
      route: '/chat',
      preloadedState: {
        dashboard: { connected: false, status: { platform: 'darwin' }, slots: [], approvalMode: 'normal' } as unknown as RootState['dashboard'],
      },
    })
    // Loud pulsing pill must be gone.
    expect(screen.queryByLabelText('Gateway offline')).toBeNull()
    // sr-only fallback present so screen readers still know the gateway
    // is unreachable; the auth banner is the canonical visible signal.
    expect(screen.getByText(/session expired, see banner above/i)).toBeTruthy()
  })

  it('flips suppression on/off live in response to mc-auth-required / mc-auth-cleared events', () => {
    renderWithProviders(<App />, {
      route: '/chat',
      preloadedState: {
        dashboard: { connected: false, status: { platform: 'darwin' }, slots: [], approvalMode: 'normal' } as unknown as RootState['dashboard'],
      },
    })
    // Initial render: no auth banner → loud pill visible.
    expect(screen.getByLabelText('Gateway offline')).toBeTruthy()

    // Simulate api/client.ts firing mc-auth-required (e.g. 403 mid-session).
    act(() => {
      window.dispatchEvent(new CustomEvent('mc-auth-required'))
    })
    expect(screen.queryByLabelText('Gateway offline')).toBeNull()
    expect(screen.getByText(/session expired, see banner above/i)).toBeTruthy()

    // User pastes a fresh token, banner removes itself, fires mc-auth-cleared.
    // App should restore the loud pill (still WS-disconnected until reconnect).
    act(() => {
      window.dispatchEvent(new CustomEvent('mc-auth-cleared'))
    })
    expect(screen.getByLabelText('Gateway offline')).toBeTruthy()
    expect(screen.queryByText(/session expired, see banner above/i)).toBeNull()
  })
})
