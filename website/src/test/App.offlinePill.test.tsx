/**
 * Test: App top-bar connection dot behavior when the auth banner is shown.
 *
 * The connection indicator lives in the unified readout capsule as a small
 * colored dot (green = connected, red = disconnected); when disconnected the
 * whole capsule tints danger. There is no "Offline" text pill.
 *
 * The offline cause is carried on THREE surfaces that must agree: the button
 * `title`, its `aria-label` (accessible name), and the sr-only `role="status"`
 * live region. The last two are the only screen-reader carriers of the cause
 * (the session-expired banner api/client.ts injects is a plain <div> with no
 * role="alert"/aria-live, so it is never announced). So when
 * `mc-auth-required` fires (or `isAuthBannerShown()` on mount) all three must
 * announce the auth-specific "session expired, see banner above" wording, not
 * the generic "Gateway offline" that points a screen-reader user at
 * reconnection when pasting a token is the fix (issue #9692).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import type { RootState } from '../store'
import { sseConnected } from '../store/dashboardSlice'
import App from '../App'

// Match the App.test.tsx mock setup. Differ only in `isAuthBannerShown`
// where each test controls it explicitly.
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

describe('App offline capsule — auth-required accessible name + live region', () => {
  beforeEach(() => {
    isAuthBannerShownMock.mockReset()
    isAuthBannerShownMock.mockReturnValue(false)
  })

  const offlineState = {
    dashboard: { connected: false, status: { platform: 'darwin' }, slots: [], approvalMode: 'normal' } as unknown as RootState['dashboard'],
  }

  // The connection dot is the capsule's only button carrying aria-expanded;
  // grab it that way so the query does not depend on the accessible name that
  // these tests are asserting varies.
  const connDot = () => {
    const btns = screen.getAllByRole('button')
    const dot = btns.find(b => b.hasAttribute('aria-expanded') && b.querySelector('[role="status"]'))
    if (!dot) throw new Error('connection dot button not found')
    return dot
  }
  const statusText = (dot: HTMLElement) => dot.querySelector('[role="status"]')?.textContent ?? ''

  it('announces the generic offline cause when WS is disconnected AND no auth banner', () => {
    renderWithProviders(<App />, { route: '/chat', preloadedState: offlineState })
    const dot = connDot()
    // Accessible name and live region carry the reconnecting cause; the
    // dot has no "Offline" text pill (the capsule's danger tint is the
    // visual signal).
    expect(dot.getAttribute('aria-label')).toMatch(/reconnecting/i)
    expect(statusText(dot)).toMatch(/reconnecting/i)
    expect(dot.getAttribute('title')).toMatch(/reconnecting/i)
    expect(dot.getAttribute('aria-label')).not.toMatch(/session expired/i)
    // The dot also collapses/expands the readouts, so its accessible name
    // names that action (matching title); the role="status" live region does
    // NOT -- a status region announces the connection cause, not the toggle
    // affordance (which would speak "collapse" on every reconnect).
    expect(dot.getAttribute('aria-label')).toMatch(/collapse readouts/i)
    expect(dot.getAttribute('aria-label')).toBe(dot.getAttribute('title'))
    expect(statusText(dot)).not.toMatch(/collapse readouts/i)
  })

  it('announces the session-expired cause (name + live region) when the auth banner is shown on mount', () => {
    isAuthBannerShownMock.mockReturnValue(true)
    renderWithProviders(<App />, { route: '/chat', preloadedState: offlineState })
    const dot = connDot()
    // The fix: the accessible name AND the role="status" live region — not
    // just the native title — say the session expired, so a screen-reader
    // user is pointed at pasting a token, not at reconnection.
    expect(dot.getAttribute('aria-label')).toMatch(/session expired, see banner above/i)
    expect(statusText(dot)).toMatch(/session expired, see banner above/i)
    expect(dot.getAttribute('title')).toMatch(/session expired, see banner above/i)
    // And the generic wording is gone from the announced surfaces.
    expect(dot.getAttribute('aria-label')).not.toMatch(/reconnecting/i)
    expect(statusText(dot)).not.toMatch(/reconnecting/i)
  })

  it('flips name + live region live in response to mc-auth-required / mc-auth-cleared events', () => {
    renderWithProviders(<App />, { route: '/chat', preloadedState: offlineState })
    expect(connDot().getAttribute('aria-label')).toMatch(/reconnecting/i)
    expect(statusText(connDot())).toMatch(/reconnecting/i)

    // Simulate api/client.ts firing mc-auth-required (e.g. 403 mid-session).
    act(() => {
      window.dispatchEvent(new CustomEvent('mc-auth-required'))
    })
    expect(connDot().getAttribute('aria-label')).toMatch(/session expired, see banner above/i)
    expect(statusText(connDot())).toMatch(/session expired, see banner above/i)

    // User pastes a fresh token, banner removes itself, fires mc-auth-cleared.
    act(() => {
      window.dispatchEvent(new CustomEvent('mc-auth-cleared'))
    })
    expect(connDot().getAttribute('aria-label')).toMatch(/reconnecting/i)
    expect(statusText(connDot())).toMatch(/reconnecting/i)
  })

  it('announces session-expired even when the transport is still connected (auth wins over connected)', () => {
    // The independently-sourced state: 403 set authRequired while the socket
    // (Redux `connected`) is still up. A transport-first order would announce
    // "Gateway connected" -- a reassuring lie, uncorrected because the banner
    // has no aria-live. Auth must take precedence for the announced cause.
    // Force both signals ON explicitly (sseConnected pins connected=true so the
    // assertion does not race the mount status fetch; mc-auth-required sets the
    // local auth flag), so the ordering is what the test measures.
    const { store } = renderWithProviders(<App />, { route: '/chat' })
    act(() => {
      store.dispatch(sseConnected())
      window.dispatchEvent(new CustomEvent('mc-auth-required'))
    })
    const dot = connDot()
    expect(dot.getAttribute('aria-label')).toMatch(/session expired, see banner above/i)
    expect(statusText(dot)).toMatch(/session expired, see banner above/i)
    // The reassuring "Gateway connected" must NOT be announced in this state.
    expect(statusText(dot)).not.toMatch(/^gateway connected/i)
    expect(dot.getAttribute('aria-label')).not.toMatch(/^gateway connected/i)
  })
})
