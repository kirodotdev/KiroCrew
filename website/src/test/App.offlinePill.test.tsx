/**
 * Test: App top-bar connection dot behavior when the auth banner is shown.
 *
 * The connection indicator lives in the unified readout capsule as a small
 * colored dot (green = connected, red = disconnected); when disconnected the
 * whole capsule tints danger. There is no "Offline" text pill, so the
 * suppression logic is just a tooltip swap: when
 * `mc-auth-required` fires (or `isAuthBannerShown()` on mount), the dot's
 * tooltip defers to the banner as the canonical signal.
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

describe('App offline capsule — auth-required tooltip', () => {
  beforeEach(() => {
    isAuthBannerShownMock.mockReset()
    isAuthBannerShownMock.mockReturnValue(false)
  })

  const offlineState = {
    dashboard: { connected: false, status: { platform: 'darwin' }, slots: [], approvalMode: 'normal' } as unknown as RootState['dashboard'],
  }

  it('shows the red connection dot when WS is disconnected AND no auth banner', () => {
    renderWithProviders(<App />, { route: '/chat', preloadedState: offlineState })
    // The unified readout capsule renders a red dot with
    // aria-label="Gateway offline"; there is no "Offline" text pill
    // (the capsule's danger tint is the disconnected signal).
    const dot = screen.getByLabelText('Gateway offline')
    expect(dot).toBeTruthy()
    expect(dot.getAttribute('title')).toMatch(/reconnecting/i)
  })

  it('points the dot tooltip at the auth banner when it is shown on mount', () => {
    isAuthBannerShownMock.mockReturnValue(true)
    renderWithProviders(<App />, { route: '/chat', preloadedState: offlineState })
    // The dot stays (quiet capsule tint, not a competing loud banner); its
    // tooltip defers to the session-expired banner as the canonical signal.
    const dot = screen.getByLabelText('Gateway offline')
    expect(dot).toBeTruthy()
    expect(dot.getAttribute('title')).toMatch(/session expired, see banner above/i)
  })

  it('flips the tooltip live in response to mc-auth-required / mc-auth-cleared events', () => {
    renderWithProviders(<App />, { route: '/chat', preloadedState: offlineState })
    expect(screen.getByLabelText('Gateway offline').getAttribute('title')).toMatch(/reconnecting/i)

    // Simulate api/client.ts firing mc-auth-required (e.g. 403 mid-session).
    act(() => {
      window.dispatchEvent(new CustomEvent('mc-auth-required'))
    })
    expect(screen.getByLabelText('Gateway offline').getAttribute('title')).toMatch(/session expired, see banner above/i)

    // User pastes a fresh token, banner removes itself, fires mc-auth-cleared.
    act(() => {
      window.dispatchEvent(new CustomEvent('mc-auth-cleared'))
    })
    expect(screen.getByLabelText('Gateway offline').getAttribute('title')).toMatch(/reconnecting/i)
  })
})
