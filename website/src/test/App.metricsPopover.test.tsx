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
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { compile } from '@tailwindcss/node'
import { renderWithProviders } from './helpers'

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
import { api } from '../api/client'

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
    vi.mocked(api.system).mockReset().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 })
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
    fireEvent.click(screen.getByLabelText(/Gateway connected/i))

    expect(screen.queryByLabelText('System metrics')).toBeNull()
    expect(screen.queryByRole('dialog', { name: 'System metrics' })).toBeNull()
  })

  it('previews the card on hover while the readings are expanded inline', async () => {
    localStorage.setItem('mc-topbar-metrics', '1')
    renderWithProviders(<App />, { route: '/chat' })
    const readout = (await screen.findByText(/CPU 25%/)).closest('button')!
    // No native title tooltip: it would pop up on top of the card.
    expect(readout.getAttribute('title')).toBeNull()
    expect(screen.queryByRole('tooltip', { name: /System metrics/ })).toBeNull()

    fireEvent.mouseEnter(readout)

    // The inline form shows percentages only; the card adds the absolute
    // figures, and names the click-to-hide the title used to carry.
    const card = await screen.findByRole('tooltip', { name: /System metrics/ })
    await waitFor(() => expect(card.textContent).toMatch(/4\/16\s*GB/))
    expect(card.textContent).toMatch(/40\/100\s*GB/)
    expect(card.textContent).toContain('Click to hide')
    // Assistive tech reaches the tooltip through the trigger, and the
    // description is the card's text: no aria-label may shadow the rows.
    expect(readout.getAttribute('aria-describedby')).toBe(card.id)
    expect(card.hasAttribute('aria-label')).toBe(false)
    expect(card.textContent).toMatch(/4\/16\s*GB[\s\S]*40\/100\s*GB/)
    // A hover preview is not a pinned dialog.
    expect(screen.queryByRole('dialog', { name: 'System metrics' })).toBeNull()

    fireEvent.mouseLeave(readout)
    await waitFor(() => expect(screen.queryByRole('tooltip', { name: /System metrics/ })).toBeNull())
    // Hovering changed no preference, and the click still hides the readout.
    expect(localStorage.getItem('mc-topbar-metrics')).toBe('1')
    fireEvent.mouseEnter(readout)
    await screen.findByRole('tooltip', { name: /System metrics/ })
    fireEvent.click(readout)
    expect(localStorage.getItem('mc-topbar-metrics')).toBe('0')
    // The click closes the card, and the pointer resting on the same control
    // raises no new mouseenter, so it does not reopen under the cursor.
    expect(screen.queryByRole('tooltip', { name: /System metrics/ })).toBeNull()
    await new Promise(r => setTimeout(r, 450))
    expect(screen.queryByRole('tooltip', { name: /System metrics/ })).toBeNull()
  })

  it('keeps age-only stale metrics as a non-error tooltip', async () => {
    localStorage.setItem('mc-topbar-metrics', '1')
    const now = vi.spyOn(Date, 'now').mockReturnValue(1_000_000)
    try {
      renderWithProviders(<App />, { route: '/chat' })
      const readout = (await screen.findByText(/CPU 25%/)).closest('button')!
      await waitFor(() => expect(vi.mocked(api.system)).toHaveBeenCalled())

      now.mockReturnValue(1_090_001)
      fireEvent.mouseEnter(readout)

      const card = await screen.findByRole('tooltip', { name: /System metrics/ })
      expect(card.querySelector('[role="alert"]')).toBeNull()
      expect(card.textContent).toContain('Last updated more than 90 seconds ago')
      expect(screen.queryByRole('dialog', { name: 'System metrics' })).toBeNull()
    } finally {
      now.mockRestore()
    }
  })

  it('names an invalid reading as unavailable in the card instead of a bare dash', async () => {
    localStorage.setItem('mc-topbar-metrics', '1')
    // A zero total cannot yield a percentage, so the frame's memory reading is
    // invalid while CPU and disk stay valid.
    vi.mocked(api.system).mockReset().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 })
    renderWithProviders(<App />, { route: '/chat' })
    const readout = (await screen.findByText(/CPU 25%/)).closest('button')!
    fireEvent.mouseEnter(readout)

    const card = await screen.findByRole('tooltip', { name: /System metrics/ })
    // The dash alone says nothing; the row's detail carries the reason.
    await waitFor(() => expect(card.textContent).toContain('Memory: unavailable'))
    expect(card.textContent).toMatch(/40\/100\s*GB/)
  })

  it('names the machine the readings belong to, with its OS and core count', async () => {
    localStorage.setItem('mc-topbar-metrics', '1')
    // Every instance tab renders this card from its own gateway, so the card
    // must say whose numbers they are. The FQDN is shortened on the line and
    // kept whole in the tooltip.
    vi.mocked(api.system).mockReset().mockResolvedValue({
      mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0,
      hostname: 'build-box-7.eu-west-1.example.com', os: 'Darwin 25.0.0', cpu_count: 10,
    })
    renderWithProviders(<App />, { route: '/chat' })
    const readout = (await screen.findByText(/CPU 25%/)).closest('button')!
    fireEvent.mouseEnter(readout)

    const card = await screen.findByRole('tooltip', { name: /System metrics/ })
    const host = await within(card).findByText('build-box-7')
    expect(host.closest('[title]')?.getAttribute('title')).toBe('build-box-7.eu-west-1.example.com')
    expect(card.textContent).toContain('macOS · 10 cores')
    // The identity follows the readings rather than displacing them.
    expect(card.textContent).toMatch(/40\/100\s*GB[\s\S]*build-box-7[\s\S]*macOS/)
  })

  it('keeps the tooltip on a long dotless hostname the line truncates', async () => {
    localStorage.setItem('mc-topbar-metrics', '1')
    // No dots means the short form is the whole name, but the line still
    // truncates at its max width, so the tooltip is the only way to read it.
    const longHost = 'dev-dsk-someone-big-1a-0123456789abcdef'
    vi.mocked(api.system).mockReset().mockResolvedValue({
      mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0,
      hostname: longHost, os: 'Linux 6.1.0', cpu_count: 96,
    })
    renderWithProviders(<App />, { route: '/chat' })
    const readout = (await screen.findByText(/CPU 25%/)).closest('button')!
    fireEvent.mouseEnter(readout)

    const card = await screen.findByRole('tooltip', { name: /System metrics/ })
    const host = await within(card).findByText(longHost)
    expect(host.closest('[title]')?.getAttribute('title')).toBe(longHost)
  })

  it('uses the singular form for one core and omits what the frame does not carry', async () => {
    localStorage.setItem('mc-topbar-metrics', '1')
    vi.mocked(api.system).mockReset().mockResolvedValue({
      mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0,
      os: 'Linux 6.1.0', cpu_count: 1,
    })
    renderWithProviders(<App />, { route: '/chat' })
    const readout = (await screen.findByText(/CPU 25%/)).closest('button')!
    fireEvent.mouseEnter(readout)

    const card = await screen.findByRole('tooltip', { name: /System metrics/ })
    await waitFor(() => expect(card.textContent).toContain('Linux · 1 core'))
    expect(card.textContent).not.toContain('1 cores')
    // No hostname in the frame: no host line, so no tooltip to carry one.
    expect(card.querySelector('[title]')).toBeNull()
  })

  it('shows no identity footer for a frame that names no machine', async () => {
    localStorage.setItem('mc-topbar-metrics', '1')
    renderWithProviders(<App />, { route: '/chat' })
    const readout = (await screen.findByText(/CPU 25%/)).closest('button')!
    fireEvent.mouseEnter(readout)

    const card = await screen.findByRole('tooltip', { name: /System metrics/ })
    await waitFor(() => expect(card.textContent).toMatch(/40\/100\s*GB/))
    // The rows are followed directly by the hint: no host line, no OS line.
    expect(card.textContent).toMatch(/40\/100\s*GB\s*\d+%\s*Click to hide$/)
  })

  it('closes the hover card on a window resize, since its anchor is measured once', async () => {
    localStorage.setItem('mc-topbar-metrics', '1')
    renderWithProviders(<App />, { route: '/chat' })
    const readout = (await screen.findByText(/CPU 25%/)).closest('button')!
    fireEvent.mouseEnter(readout)
    await screen.findByRole('tooltip', { name: /System metrics/ })
    act(() => { window.dispatchEvent(new Event('resize')) })
    await waitFor(() => expect(screen.queryByRole('tooltip', { name: /System metrics/ })).toBeNull())
  })

  it('shows a failed metrics refetch in the expanded-readout hover dialog', async () => {
    localStorage.setItem('mc-topbar-metrics', '1')
    vi.mocked(api.system)
      .mockResolvedValueOnce({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 })
      .mockRejectedValue(new Error('metrics unavailable'))
    const { queryClient } = renderWithProviders(<App />, { route: '/chat' })
    const readout = (await screen.findByText(/CPU 25%/)).closest('button')!

    await act(async () => {
      await queryClient.refetchQueries({ queryKey: ['system-metrics'] })
    })
    expect(queryClient.getQueryState(['system-metrics'])?.status).toBe('error')

    fireEvent.mouseEnter(readout)

    const card = await screen.findByRole('dialog', { name: 'System metrics' })
    expect(card.textContent).toMatch(/4\/16\s*GB/)
    expect(card.querySelector('[role="alert"]')?.textContent).toContain('The last metrics update failed')
    // The dialog names itself, so the trigger must not also be described by it.
    expect(readout.getAttribute('aria-describedby')).toBeNull()
  })

  it('shows a failed metrics fetch in the bare-icon hover dialog', async () => {
    vi.mocked(api.system).mockRejectedValue(new Error('metrics unavailable'))
    const { queryClient } = renderWithProviders(<App />, { route: '/chat' })
    const btn = await screen.findByLabelText('System metrics')

    await waitFor(() => expect(queryClient.getQueryState(['system-metrics'])?.status).toBe('error'))
    fireEvent.mouseEnter(btn)

    const card = await screen.findByRole('dialog', { name: 'System metrics' })
    expect(card.querySelector('[role="alert"]')?.textContent).toContain('The last metrics update failed')
    expect(btn.getAttribute('aria-describedby')).toBeNull()
  })

  it('previews the card on hover in the collapsed band, and a click pins it as a dialog', async () => {
    injected = collapseTheLadder()
    renderWithProviders(<App />, { route: '/chat' })
    const btn = await screen.findByLabelText('System metrics')

    fireEvent.mouseEnter(btn)
    const card = await screen.findByRole('tooltip', { name: /System metrics/ })
    expect(card.textContent).toContain('CPU')
    // Only the expanded readout hides on click, so only it advertises that.
    expect(card.textContent).not.toContain('Click to hide')

    fireEvent.click(btn)
    await screen.findByRole('dialog', { name: 'System metrics' })
    expect(screen.queryByRole('tooltip', { name: /System metrics/ })).toBeNull()
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

  it('renders the same contents in both bands, changing only what a click does', async () => {
    // The desktop top bar picks the band by measuring these contents
    // (lib/useTopbarCollapse.ts). A control that rendered other contents in the
    // collapsed band made the narrower form fit the wider band, which brought the
    // readings back, which no longer fit: the bar flipped between the two forever.
    const shapeOf = async () => {
      const btn = await screen.findByRole('button', { name: /System metrics/ })
      await waitFor(() => expect(btn.textContent).toMatch(/CPU\s*25/))
      const shape = [...btn.querySelectorAll('*')].map(n => `${n.tagName}.${n.getAttribute('class') ?? ''}`)
      return { shape, haspopup: btn.getAttribute('aria-haspopup') }
    }
    localStorage.setItem('mc-topbar-metrics', '1')
    const wide = renderWithProviders(<App />, { route: '/chat' })
    const inline = await shapeOf()
    wide.unmount()

    localStorage.setItem('mc-topbar-metrics', '1')
    injected = collapseTheLadder()
    renderWithProviders(<App />, { route: '/chat' })
    const collapsed = await shapeOf()

    expect(inline.haspopup).toBeNull()
    expect(collapsed.haspopup).toBe('dialog')
    expect(collapsed.shape).toEqual(inline.shape)
  })

  it('renders a failed fetch as a sibling notice after the capsule, with a plain inline toggle', async () => {
    vi.mocked(api.system).mockRejectedValue(new Error('metrics unavailable'))
    localStorage.setItem('mc-topbar-metrics', '1')
    const { queryClient } = renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(queryClient.getQueryState(['system-metrics'])?.status).toBe('error'))

    const notice = await screen.findByTestId('topbar-metrics-error')
    const right = notice.closest('.tb-right')!
    const capsule = right.querySelector('.tb-capsule')!
    expect(notice.getAttribute('role')).toBe('alert')
    expect(notice.textContent).toContain('metrics unavailable')
    expect(within(notice).getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
    expect(capsule.contains(notice)).toBe(false)
    expect(notice.previousElementSibling?.querySelector('.tb-capsule')).toBe(capsule)
    // Natural width: the ladder makes room for the notice, so nothing truncates
    // its message and the message needs no tooltip.
    expect(notice.classList.contains('shrink-0')).toBe(true)
    expect(notice.classList.contains('min-w-0')).toBe(false)
    const msg = notice.querySelector('.tb-metrics-notice-msg')!
    expect(msg.textContent).toBe('metrics unavailable')
    expect(msg.hasAttribute('title')).toBe(false)

    const btn = within(capsule).getByRole('button', { name: 'System metrics' })
    expect(btn.classList.contains('text-danger')).toBe(false)
    expect(btn.textContent).not.toMatch(/metrics unavailable/i)
    expect(btn.getAttribute('aria-pressed')).toBe('true')

    fireEvent.click(btn)

    await waitFor(() => expect(localStorage.getItem('mc-topbar-metrics')).toBe('0'))
    expect(screen.queryByTestId('topbar-metrics-error')).toBeNull()
  })

  it('keeps a failed-fetch notice visible in the collapsed band while the capsule control opens the card', async () => {
    vi.mocked(api.system).mockRejectedValue(new Error('metrics unavailable'))
    localStorage.setItem('mc-topbar-metrics', '1')
    injected = collapseTheLadder()
    const { queryClient } = renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(queryClient.getQueryState(['system-metrics'])?.status).toBe('error'))
    const btn = await screen.findByRole('button', { name: 'System metrics' })
    expect(btn.getAttribute('aria-haspopup')).toBe('dialog')
    expect(btn.classList.contains('text-danger')).toBe(false)
    expect(btn.textContent).not.toMatch(/metrics unavailable/i)
    expect(screen.getByTestId('topbar-metrics-error')).toBeInTheDocument()

    fireEvent.click(btn)

    const card = await screen.findByRole('dialog', { name: 'System metrics' })
    expect(card.querySelector('[role="alert"]')?.textContent).toContain('The last metrics update failed')
    expect(localStorage.getItem('mc-topbar-metrics')).toBe('1')
    expect(screen.getByTestId('topbar-metrics-error')).toBeInTheDocument()
  })

  it('keeps the failed-fetch control and notice descendant shapes identical in both bands', async () => {
    vi.mocked(api.system).mockRejectedValue(new Error('metrics unavailable'))
    localStorage.setItem('mc-topbar-metrics', '1')
    const shapeOf = async () => {
      const notice = await screen.findByTestId('topbar-metrics-error')
      const btn = screen.getByRole('button', { name: 'System metrics' })
      const descendants = (root: Element) => [...root.querySelectorAll('*')].map(n => `${n.tagName}.${n.getAttribute('class') ?? ''}`)
      return { control: descendants(btn), notice: descendants(notice), haspopup: btn.getAttribute('aria-haspopup') }
    }

    const wide = renderWithProviders(<App />, { route: '/chat' })
    const inline = await shapeOf()
    wide.unmount()

    localStorage.setItem('mc-topbar-metrics', '1')
    injected = collapseTheLadder()
    renderWithProviders(<App />, { route: '/chat' })
    const collapsed = await shapeOf()

    expect(inline.haspopup).toBeNull()
    expect(collapsed.haspopup).toBe('dialog')
    expect(collapsed.control).toEqual(inline.control)
    expect(collapsed.notice).toEqual(inline.notice)
  })

  it('hides the failed-fetch notice while readings are off, the capsule is collapsed, or the first fetch is pending', async () => {
    vi.mocked(api.system).mockRejectedValue(new Error('metrics unavailable'))
    const off = renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(off.queryClient.getQueryState(['system-metrics'])?.status).toBe('error'))
    expect(screen.queryByTestId('topbar-metrics-error')).toBeNull()
    off.unmount()

    localStorage.setItem('mc-topbar-metrics', '1')
    localStorage.setItem('mc-topbar-capsule-collapsed', '1')
    const collapsed = renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(collapsed.queryClient.getQueryState(['system-metrics'])?.status).toBe('error'))
    expect(screen.queryByLabelText('System metrics')).toBeNull()
    expect(screen.queryByTestId('topbar-metrics-error')).toBeNull()
    collapsed.unmount()

    localStorage.setItem('mc-topbar-capsule-collapsed', '0')
    vi.mocked(api.system).mockReset().mockReturnValue(new Promise(() => {}))
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByLabelText(/Gateway connected/i)
    expect(screen.queryByTestId('topbar-metrics-error')).toBeNull()
  })

  // At the rung that drops the readings the glyph is all that is visible of an
  // open readout, so it has to show whether the card is pinned. jsdom applies no
  // Tailwind, so these assert the contract that produces the colour: the button
  // is the `group`, its `aria-expanded` follows the pin, and the glyph carries
  // the group variant that reads it. The classes are the same in both bands
  // (the band-invariance test above), and the inline band sets no
  // `aria-expanded`, so the variant has nothing to match there.
  it('tints the collapsed-band glyph by the pinned state while the readings are loaded', async () => {
    localStorage.setItem('mc-topbar-metrics', '1')
    injected = collapseTheLadder()
    renderWithProviders(<App />, { route: '/chat' })
    const btn = await screen.findByRole('button', { name: /System metrics/ })
    await waitFor(() => expect(btn.textContent).toMatch(/CPU\s*25/))
    const glyph = btn.querySelector('.tb-narrow-only')!
    expect(btn.classList.contains('group')).toBe(true)
    expect(glyph.classList.contains('group-aria-[expanded=false]:!text-muted')).toBe(true)
    expect(glyph.classList.contains('text-accent')).toBe(true)
    expect(btn.getAttribute('aria-expanded')).toBe('false')

    fireEvent.click(btn)
    await screen.findByRole('dialog', { name: 'System metrics' })
    expect(btn.getAttribute('aria-expanded')).toBe('true')

    fireEvent.click(btn)
    expect(btn.getAttribute('aria-expanded')).toBe('false')
  })

  it('tints the failed-fetch glyph by the pinned state without danger text', async () => {
    vi.mocked(api.system).mockRejectedValue(new Error('metrics unavailable'))
    localStorage.setItem('mc-topbar-metrics', '1')
    injected = collapseTheLadder()
    const { queryClient } = renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(queryClient.getQueryState(['system-metrics'])?.status).toBe('error'))
    const btn = await screen.findByRole('button', { name: 'System metrics' })
    const glyph = btn.querySelector('svg')!
    expect(btn.classList.contains('group')).toBe(true)
    expect(btn.classList.contains('text-danger')).toBe(false)
    expect(glyph.classList.contains('tb-narrow-only')).toBe(false)
    expect(glyph.classList.contains('text-accent')).toBe(true)
    expect(glyph.classList.contains('group-aria-[expanded=false]:!text-muted')).toBe(true)
    expect(btn.getAttribute('aria-expanded')).toBe('false')

    fireEvent.click(btn)
    await screen.findByRole('dialog', { name: 'System metrics' })
    expect(btn.getAttribute('aria-expanded')).toBe('true')
    expect(btn.classList.contains('text-danger')).toBe(false)
    expect(screen.getByTestId('topbar-metrics-error')).toBeInTheDocument()
  })

  it('leaves the inline band without aria-expanded, so the pinned tint cannot apply there', async () => {
    localStorage.setItem('mc-topbar-metrics', '1')
    renderWithProviders(<App />, { route: '/chat' })
    const btn = await screen.findByRole('button', { name: /System metrics/ })
    await waitFor(() => expect(btn.textContent).toMatch(/CPU\s*25/))
    expect(btn.getAttribute('aria-expanded')).toBeNull()
    expect(btn.getAttribute('aria-haspopup')).toBeNull()
    expect(btn.classList.contains('group')).toBe(true)
    expect(btn.querySelector('.tb-narrow-only')!.classList.contains('group-aria-[expanded=false]:!text-muted')).toBe(true)
  })
})

// happy-dom runs no cascade, so the tests above prove the classes, not the
// colour. A theme that overrides `.text-accent` unlayered in index.css
// (kiro-dark) matches the glyph at the same specificity as the compiled
// `group-aria-[expanded=false]:` rule and comes after it, so the override wins
// and the unpinned glyph paints accent. Only `!important` on the muted rule
// beats it, so this compiles the glyph's real classes and checks that.
describe('collapsed-band metrics glyph against theme accent overrides', () => {
  const WEBSITE = join(__dirname, '..', '..')
  const source = readFileSync(join(WEBSITE, 'src', 'shell', 'topbar', 'metricsReadout.tsx'), 'utf8')
  const indexCss = readFileSync(join(WEBSITE, 'src', 'index.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '')

  it('mutes the unpinned glyph with a rule no theme `.text-accent` override can beat', async () => {
    const overrides = indexCss.match(/\[data-theme="[^"]+"\]\s+\.text-accent\{color:[^}]*\}/g) ?? []
    expect(overrides.length, 'the kiro-dark .text-accent override this guards against').toBeGreaterThan(0)
    for (const rule of overrides) expect(rule).not.toContain('!important')

    const glyphClasses = [...source.matchAll(/<AudioWaveform[^>]*className="([^"]*)"/g)].map((m) => m[1])
    const mutedTokens = glyphClasses.flatMap((c) => c.split(/\s+/)).filter((t) => t.startsWith('group-aria-[expanded=false]:'))
    expect(mutedTokens.length, 'one muted variant per open-form glyph').toBe(3)

    const compiler = await compile(
      ['@import "tailwindcss/theme.css" layer(theme);', '@import "./src/tailwind-theme.css";', '@tailwind utilities source(none);'].join('\n'),
      { base: WEBSITE, onDependency() {} },
    )
    const css = compiler.build(mutedTokens)
    const rules = css.match(/\.group-aria-[^{]*\{[^}]*\}/g) ?? []
    expect(rules.length).toBeGreaterThan(0)
    for (const rule of rules) expect(rule).toMatch(/color:\s*var\(--muted\)\s*!important/)
  })
})
