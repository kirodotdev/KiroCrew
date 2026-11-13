/**
 * Windows window drag over the docked header.
 *
 * Transcript content scrolled up under the glass header reaches into the title
 * band from layout, and those `no-drag` rectangles come after the header's own
 * `drag` region, so they cut holes in it. The `.host-drag-strip` gap strips are
 * painted after everything and re-add drag over the band's control-free gaps.
 * On Windows they must stop short of the caption buttons, or a strip over Close
 * moves the window instead of closing it. jsdom has no native window, so this
 * pins the mount and the right edge; the drag itself is checked on the app.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { __resetFocusMode } from '../hooks/useFocusMode'
import { WIN_CAPTION_OVERLAY_WIDTH } from '../lib/electron'

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
// The platform flags are module-level consts frozen at import.
vi.mock('../lib/electron', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/electron')>()),
  isElectron: true,
  isWinElectron: true,
  isMacElectron: false,
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

describe('Windows header drag strips', () => {
  beforeEach(() => {
    __resetFocusMode()
    localStorage.clear()
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 1400 })
  })
  afterEach(() => {
    vi.useRealTimers()
    document.documentElement.style.removeProperty('--mc-win-drag-reserve')
  })

  it('covers the docked header up to the caption buttons, and unmounts while focus mode hides it', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    const toggle = await screen.findByTestId('focus-mode-toggle')

    // jsdom lays nothing out, so the controls have no box and one gap spans the
    // whole band, which ends exactly where the caption buttons start.
    const strips = screen.getAllByTestId('header-drag-strip')
    expect(strips).toHaveLength(1)
    expect(parseFloat(strips[0].style.left)).toBe(0)
    expect(parseFloat(strips[0].style.width)).toBe(1400 - WIN_CAPTION_OVERLAY_WIDTH)

    await act(async () => { fireEvent.click(toggle) })
    expect(screen.queryByTestId('header-drag-strip')).toBeNull()
  })

  // Below 100% zoom the fixed-DIP caption buttons are wider in CSS px, and the
  // main process pushes that width onto <html>. A strip measured with the old
  // value would reach over Minimize, so the push re-measures.
  it('stops at the caption reserve the main process pushes for the zoom', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('focus-mode-toggle')
    vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame'] })
    // Settle every pending re-measure, so only the push below can move the strip.
    await act(async () => {})
    act(() => { vi.advanceTimersToNextFrame() })
    expect(parseFloat(screen.getByTestId('header-drag-strip').style.width)).toBe(1400 - WIN_CAPTION_OVERLAY_WIDTH)
    document.documentElement.style.setProperty('--mc-win-drag-reserve', '173px')
    await Promise.resolve() // deliver the mutation record
    act(() => { vi.advanceTimersToNextFrame() })
    const strips = screen.getAllByTestId('header-drag-strip')
    expect(strips).toHaveLength(1)
    expect(parseFloat(strips[0].style.width)).toBe(1400 - 173)
  })
})
