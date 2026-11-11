/**
 * Test: App top-bar connection dot behavior when the auth banner is shown.
 *
 * The connection indicator lives in the unified readout capsule as a small
 * glyph whose shape carries the state (plug = connected, unplugged plug =
 * disconnected, colour as a second cue); when disconnected the whole capsule
 * tints danger. There is no "Offline" text pill.
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
import { act, screen, waitFor, within } from '@testing-library/react'
import type { QueryClient } from '@tanstack/react-query'
import { renderWithProviders } from './helpers'
import { namedCeiling } from './namedCeiling'
import type { RootState } from '../store'
import { sseConnected, sseDisconnected } from '../store/dashboardSlice'
import App from '../App'
import en from '../i18n/locales/en.manual.json'

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
    // ThemeProvider's catalog query retries every 1s, 2s, 4s... until a read succeeds, and each failed retry notifies it.
    themes: vi.fn().mockResolvedValue({ themes: [] }),
    // The mates dock's roster read retries 1s, 2s, 4s through a failure, past SHELL_SETTLED, so it is answered.
    members: vi.fn().mockResolvedValue({ members: [] }),
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

// These tests assert on the shell's first frame, before the mount-time status read flips `connected`, so they
// cannot wait first. Everything else the shell starts at mount (apps, instances, slots, the theme boot, about ten
// queries this mock leaves unanswered, the 600ms timer that retires the entrance class) lands after that frame,
// and without a wait it lands after the test. Each test ends here: the entrance class gone and every query idle.
const SHELL_SETTLED = namedCeiling('SHELL_SETTLED', 5000)
const shellSettled = (queryClient: QueryClient) =>
  waitFor(() => {
    expect(screen.getByTestId('dashboard-shell')).not.toHaveClass('animate-rise')
    expect(queryClient.isFetching()).toBe(0)
  }, SHELL_SETTLED)

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

  it('announces the generic offline cause when WS is disconnected AND no auth banner', async () => {
    const { queryClient } = renderWithProviders(<App />, { route: '/chat', preloadedState: offlineState })
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
    await shellSettled(queryClient)
  })

  it('announces the session-expired cause (name + live region) when the auth banner is shown on mount', async () => {
    isAuthBannerShownMock.mockReturnValue(true)
    const { queryClient } = renderWithProviders(<App />, { route: '/chat', preloadedState: offlineState })
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
    await shellSettled(queryClient)
  })

  it('flips name + live region live in response to mc-auth-required / mc-auth-cleared events', async () => {
    const { queryClient } = renderWithProviders(<App />, { route: '/chat', preloadedState: offlineState })
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
    await shellSettled(queryClient)
  })

  it('announces session-expired even when the transport is still connected (auth wins over connected)', async () => {
    // The independently-sourced state: 403 set authRequired while the socket
    // (Redux `connected`) is still up. A transport-first order would announce
    // "Gateway connected" -- a reassuring lie, uncorrected because the banner
    // has no aria-live. Auth must take precedence for the announced cause.
    // Force both signals ON explicitly (sseConnected pins connected=true so the
    // assertion does not race the mount status fetch; mc-auth-required sets the
    // local auth flag), so the ordering is what the test measures.
    const { store, queryClient } = renderWithProviders(<App />, { route: '/chat' })
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
    await shellSettled(queryClient)
  })
})

describe('App connection indicator — the shape carries the state, not colour alone', () => {
  // WCAG 1.4.1: colour must not be the only visual cue. The glyph inside the
  // connection button is a plug when connected and an unplugged plug when
  // offline, so a reader who cannot tell red from green still sees the state.
  const connGlyph = () => {
    const btn = screen.getAllByRole('button').find(b => b.hasAttribute('aria-expanded') && b.querySelector('[role="status"]'))
    if (!btn) throw new Error('connection button not found')
    const glyph = btn.querySelector('svg')
    if (!glyph) throw new Error('connection glyph not found')
    return glyph
  }

  it('shows a plug icon when the gateway is connected', async () => {
    const { store, queryClient } = renderWithProviders(<App />, { route: '/chat' })
    act(() => { store.dispatch(sseConnected()) })
    const glyph = connGlyph()
    expect(glyph.getAttribute('aria-hidden')).toBe('true')
    expect(glyph.getAttribute('data-conn-state')).toBe('connected')
    expect(glyph.classList.contains('lucide-plug')).toBe(true)
    expect(glyph.classList.contains('lucide-unplug')).toBe(false)
    await shellSettled(queryClient)
  })

  it('shows an unplugged icon when offline, and its pulse stops under reduced motion', async () => {
    const { queryClient } = renderWithProviders(<App />, {
      route: '/chat',
      preloadedState: { dashboard: { connected: false, status: { platform: 'darwin' }, slots: [], approvalMode: 'normal' } as unknown as RootState['dashboard'] },
    })
    const glyph = connGlyph()
    expect(glyph.getAttribute('aria-hidden')).toBe('true')
    expect(glyph.getAttribute('data-conn-state')).toBe('offline')
    expect(glyph.classList.contains('lucide-unplug')).toBe(true)
    expect(glyph.classList.contains('lucide-plug')).toBe(false)
    expect(glyph.classList.contains('motion-reduce:animate-none')).toBe(true)
    await shellSettled(queryClient)
  })
})

describe('App offline notice — visible at desktop width, worded by the cause (#34355)', () => {
  beforeEach(() => {
    isAuthBannerShownMock.mockReset()
    isAuthBannerShownMock.mockReturnValue(false)
  })

  it.each([
    [undefined, en.app.gateway_offline_reconnecting],
    ['session_ended', en.app.relay_session_ended],
    ['refused', en.app.relay_access_refused],
  ] as const)('shows the cause %s in words beside the connection dot', async (cause, text) => {
    const { store, queryClient } = renderWithProviders(<App />, { route: '/chat' })
    // Explicit transport state: the mount-time status read turns `connected`
    // on, and whether it lands first is a race.
    act(() => { store.dispatch(sseDisconnected(cause)) })
    const notice = screen.getByTestId('topbar-offline-notice')
    expect(notice).toHaveTextContent(text)
    // In flow in the actions group, where the collapse ladder makes room for
    // it, not hanging below the bar where the rail and panes paint over it.
    expect(notice.closest('.tb-right')).not.toBeNull()
    expect(notice.querySelector('.tb-offline-notice-msg')).toHaveTextContent(text)
    // The shared error surface (ErrorNotice, inline), whose icon rung tbr-6
    // drops by the `.tb-offline-notice` class, with no hand-off: the gateway
    // it would hand to is the thing that is down.
    expect(notice).toHaveClass('tb-offline-notice')
    expect(notice.querySelector(':scope > svg')).not.toBeNull()
    expect(within(notice).queryByRole('button')).toBeNull()
    // The dot's sr-only span is the live region; the notice is not a second one.
    expect(notice).not.toHaveAttribute('role')
    // The phone strip stays a phone-only element.
    expect(screen.queryByTestId('mobile-offline-strip')).toBeNull()
    act(() => { store.dispatch(sseConnected()) })
    expect(screen.queryByTestId('topbar-offline-notice')).toBeNull()
    await shellSettled(queryClient)
  })

  it('stays hidden while the session-expired banner carries the message', async () => {
    isAuthBannerShownMock.mockReturnValue(true)
    const { store, queryClient } = renderWithProviders(<App />, { route: '/chat' })
    act(() => { store.dispatch(sseDisconnected()) })
    expect(screen.queryByTestId('topbar-offline-notice')).toBeNull()
    await shellSettled(queryClient)
  })
})
