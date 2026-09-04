/**
 * Top-bar metrics control: what a click does depends on whether the collapse
 * ladder is currently rendering the inline readings.
 *
 * The ladder is a CSS container query (index.css), and its metrics rung shifts
 * while the update pill is mounted. In the shifted band the readings are
 * `display:none`, so the open/closed preference has nothing to render and the
 * old toggle was a visible no-op: the icon changed colour and nothing expanded.
 * The control now opens an anchored popover there instead.
 *
 * jsdom does not evaluate `@container`, so the probe reads as visible by default
 * and the fits-branch tests exercise the unchanged toggle. The collapsed band is
 * reproduced by giving the probe the same `display:none` the rung would, which
 * is exactly the signal the component reads.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
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
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

import App from '../App'

/** Reproduce the rung's own verdict: the readings (and so the probe that
 *  carries their class) are dropped at this width. */
function collapseTheLadder() {
  const style = document.createElement('style')
  style.id = 'test-ladder-rung'
  style.textContent = '.tb-drop-metrics{display:none}'
  document.head.appendChild(style)
  return style
}

describe('top-bar metrics control — collapsed band opens a popover', () => {
  let injected: HTMLStyleElement | null = null

  beforeEach(() => {
    localStorage.clear()
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 1400 })
  })
  afterEach(() => {
    injected?.remove()
    injected = null
  })

  it('opens a popover with the readings instead of writing a preference nothing renders', async () => {
    injected = collapseTheLadder()
    renderWithProviders(<App />, { route: '/chat' })
    const btn = await screen.findByLabelText('System metrics')
    // The trigger advertises a popover, not a pressed toggle: with the readings
    // dropped there is no inline state for `aria-pressed` to describe.
    expect(btn.getAttribute('aria-haspopup')).toBe('dialog')
    expect(btn.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByRole('dialog', { name: 'System metrics' })).toBeNull()

    fireEvent.click(btn)

    const popover = await screen.findByRole('dialog', { name: 'System metrics' })
    expect(btn.getAttribute('aria-expanded')).toBe('true')
    // Every reading the inline form would have shown, including the absolute
    // memory and disk figures that inline only carries in a tooltip.
    for (const label of ['CPU', 'MEM', 'DSK']) {
      expect(popover.textContent).toContain(label)
    }
    // used/total, unit on the total only, both sides through the i18n number
    // helpers -- so this asserts the localized shape, not a hand-built string.
    // fmtUnit glues the digits to the unit with U+00A0, hence the \s.
    await waitFor(() => expect(popover.textContent).toMatch(/4\/16\s*GB/))
    expect(popover.textContent).toMatch(/40\/100\s*GB/)
    // The preference describes the INLINE readout, which this band cannot show,
    // so the click must leave it alone.
    expect(localStorage.getItem('mc-topbar-metrics')).toBeNull()

    // Clicking again closes it — a toggle that visibly toggles.
    fireEvent.click(btn)
    expect(screen.queryByRole('dialog', { name: 'System metrics' })).toBeNull()
    expect(btn.getAttribute('aria-expanded')).toBe('false')
  })

  it('dismisses on Escape and on a click outside, returning focus to the trigger on Escape', async () => {
    injected = collapseTheLadder()
    renderWithProviders(<App />, { route: '/chat' })
    const btn = await screen.findByLabelText('System metrics')

    fireEvent.click(btn)
    await screen.findByRole('dialog', { name: 'System metrics' })
    // Escape is the keyboard dismissal, so it hands focus back to the trigger.
    // Asserted on the call, not on document.activeElement: other mounted
    // surfaces in this shell autofocus their own heading, so the winner of a
    // real focus race says nothing about whether this path restored focus.
    const focusSpy = vi.spyOn(btn, 'focus')
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'System metrics' })).toBeNull()
    expect(focusSpy).toHaveBeenCalled()
    focusSpy.mockRestore()

    fireEvent.click(btn)
    await screen.findByRole('dialog', { name: 'System metrics' })
    fireEvent.pointerDown(document.body)
    expect(screen.queryByRole('dialog', { name: 'System metrics' })).toBeNull()
  })

  it('focuses the popover on open so its readings are reachable without traversing the document', async () => {
    injected = collapseTheLadder()
    renderWithProviders(<App />, { route: '/chat' })
    const btn = await screen.findByLabelText('System metrics')

    // Recorded on the call rather than read off document.activeElement: other
    // surfaces in this shell autofocus their own heading as the route mounts, so
    // whoever wins a real focus race says nothing about whether this path moved
    // the caret.
    const focused: HTMLElement[] = []
    const focusSpy = vi.spyOn(HTMLElement.prototype, 'focus').mockImplementation(function (this: HTMLElement) {
      focused.push(this)
    })
    try {
      fireEvent.click(btn)

      // The portal renders at the end of <body>, so leaving the caret on the
      // trigger means a screen reader reaches the readings only by traversing
      // there. tabIndex={-1} is what makes the move possible without putting a
      // transient readout in the tab ring.
      const popover = await screen.findByRole('dialog', { name: 'System metrics' })
      expect(popover.getAttribute('tabindex')).toBe('-1')
      expect(focused).toContain(popover)
    } finally {
      focusSpy.mockRestore()
    }
  })

  it('closes the popover when the capsule collapses, since that unmounts the trigger', async () => {
    injected = collapseTheLadder()
    renderWithProviders(<App />, { route: '/chat' })
    const btn = await screen.findByLabelText('System metrics')

    fireEvent.click(btn)
    await screen.findByRole('dialog', { name: 'System metrics' })

    // The connection dot folds the capsule down to itself, which unmounts every
    // readout including this trigger. A popover left open would then be anchored
    // to a box that no longer exists, with nothing on screen owning it.
    fireEvent.click(screen.getByLabelText('Gateway connected'))

    expect(screen.queryByLabelText('System metrics')).toBeNull()
    expect(screen.queryByRole('dialog', { name: 'System metrics' })).toBeNull()
  })

  it('keeps the inline toggle unchanged while the readings still fit', async () => {
    // No injected rung: the probe is visible, so this is the wide-group path.
    renderWithProviders(<App />, { route: '/chat' })
    const btn = await screen.findByLabelText('System metrics')
    expect(btn.getAttribute('aria-pressed')).toBe('false')
    expect(btn.getAttribute('aria-haspopup')).toBeNull()

    fireEvent.click(btn)

    // Writes the preference and expands inline — no popover in this band.
    expect(localStorage.getItem('mc-topbar-metrics')).toBe('1')
    expect(screen.queryByRole('dialog', { name: 'System metrics' })).toBeNull()
  })
})
