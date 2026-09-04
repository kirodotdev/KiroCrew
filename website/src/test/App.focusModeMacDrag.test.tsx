/**
 * Focus mode, macOS window drag.
 *
 * The revealed header is meant to move the window from its empty regions. On
 * Windows/Linux the header carries a static/class-driven `-webkit-app-region:
 * drag`; on macOS every drag surface attached INSIDE the shell failed to
 * register with the native window on the desktop app (the class-toggled region
 * on the transformed header, a full-width mount strip, in-shell gap strips), so
 * macOS drag is carried by `.host-drag-strip` divs over the header's
 * control-free gaps, rendered OUTSIDE the shell in the pane-stack container —
 * the same mechanism, class, and DOM position that provably works for remote
 * panes. This file pins that shape: strips mount only while the header is on
 * screen, live outside the shell, and unmount positionally. jsdom applies no
 * CSS and has no native window, so the lifecycle and placement are the parts a
 * test can hold; the drag itself was verified on the desktop app.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { __resetFocusMode } from '../hooks/useFocusMode'

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))
// isMacElectron is a module-level const frozen at import — force it true so the
// macOS-only drag strip is exercisable, preserving the module's other exports.
vi.mock('../lib/electron', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/electron')>()),
  isElectron: true,
  isMacElectron: true,
}))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { available: false } }),
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
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import App from '../App'

describe('focus mode — macOS window drag strip', () => {
  beforeEach(() => {
    __resetFocusMode()
    localStorage.clear()
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 1400 })
  })

  it('mounts a drag surface only while the peeked header is on screen', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    const toggle = await screen.findByTestId('focus-mode-toggle')

    // Docked: the header carries its own region, so no separate strip.
    expect(screen.queryByTestId('focus-mac-drag-strip')).toBeNull()

    // Focus on but header hidden: nothing to drag, and a strip left mounted here
    // would be a 42px drag region over the content focus mode just reclaimed.
    await act(async () => { fireEvent.click(toggle) })
    expect(screen.queryByTestId('focus-mac-drag-strip')).toBeNull()

    // Peeked: the strip mounts. The mount is the region toggle — macOS ignores an
    // in-place `-webkit-app-region` flip on the transformed header, so the drag
    // surface has to enter the layout fresh each time the header appears.
    vi.useFakeTimers()
    try {
      fireEvent.mouseEnter(screen.getByTestId('focus-peek-top'))
      act(() => { vi.advanceTimersByTime(150) })

      const strip = screen.getByTestId('focus-mac-drag-strip')
      expect(strip.className).toContain('host-drag-strip')

      // OUTSIDE the shell, deliberately: every drag surface rendered inside the
      // shell (class-toggled header region, full-width strip, in-shell gap
      // strips) failed to register with the macOS window; the pane strips in
      // the top-level container are the one mechanism proven to work, so the
      // local strips must live in the same structural position.
      const shell = screen.getByTestId('dashboard-shell')
      expect(shell.contains(strip)).toBe(false)

      // Header hides again (past the close grace): the strip must leave the
      // layout, or the reclaimed top band stays a drag region and swallows the
      // hover that summons the header. Closing is POSITIONAL (departWhen): a
      // mouseleave is event silence a drag region can forge, so only a
      // mousemove observed below the header band closes it. Timers stay FAKE —
      // the close grace is scheduled on this clock.
      act(() => {
        document.dispatchEvent(new MouseEvent('mousemove', { clientY: 300, bubbles: true }))
      })
      act(() => { vi.advanceTimersByTime(400) })
    } finally {
      vi.useRealTimers()
    }
    expect(screen.queryByTestId('focus-mac-drag-strip')).toBeNull()
  })

  it('opens the overlay when the pointer overshoots OUT of the window through its edge', async () => {
    // Overshooting the trigger straight off the window fires mouseleave on the
    // way out, which the dwell logic reads as a cancel — yet the slam is the
    // strongest statement of intent there is (same gesture as revealing the
    // macOS Dock). A document mouseout with relatedTarget null carries the last
    // in-window coordinates: a small clientX means the pointer left through the
    // LEFT edge, a small clientY through the TOP. Ungated on the shell — the
    // browser/embedded-pane case is pinned in App.focusMode.test.tsx; this
    // suite pins it under the Electron mock.
    renderWithProviders(<App />, { route: '/chat' })
    const toggle = await screen.findByTestId('focus-mode-toggle')
    await act(async () => { fireEvent.click(toggle) })

    const rail = screen.getByRole('navigation', { name: 'Main navigation' })
    const header = document.querySelector('header.topbar') as HTMLElement
    expect(rail.style.transform).toBe('translateX(calc(-100% - 12px))')

    // In-window travel near the edge (relatedTarget is an element, not null)
    // must NOT summon anything — only a genuine exit does.
    await act(async () => {
      document.dispatchEvent(new MouseEvent('mouseout', {
        bubbles: true, relatedTarget: document.body, clientX: 4, clientY: 400,
      }))
    })
    expect(rail.style.transform).toBe('translateX(calc(-100% - 12px))')

    // Out through the LEFT edge → the rail opens immediately, no dwell. Not the
    // header: clientY is deep in the window, so this was not a top-edge exit.
    await act(async () => {
      document.dispatchEvent(new MouseEvent('mouseout', {
        bubbles: true, relatedTarget: null, clientX: 4, clientY: 400,
      }))
    })
    expect(rail.style.transform).toBe('translateX(0)')
    expect(header.style.transform).toBe('translateY(-100%)')

    // Out through the TOP edge → the header opens (corner exits prefer it).
    await act(async () => {
      document.dispatchEvent(new MouseEvent('mouseout', {
        bubbles: true, relatedTarget: null, clientX: 400, clientY: 3,
      }))
    })
    expect(header.style.transform).toBe('translateY(0)')
  })
})
