/**
 * The metrics readout must keep its toggle in EVERY open state — including the
 * one where no frame has arrived yet.
 *
 * The capsule's metrics readout is an inline segment, not a popover: the same
 * button both shows the readings and toggles them away. The open branch used to
 * render a segment only when `sysMetricsError` was set, so the third state —
 * open, no frame, no error — pushed nothing. `sysMetrics` is undefined for the
 * whole of the first fetch AND for the retry window of a failing one (react-query
 * reports `isError` only after its retries are spent), so on a slow or hanging
 * `/api/system` the toggle simply vanished from the capsule: the readout was
 * logically "open" with nothing on screen to close it. That is the reported "the
 * metrics doesn't open on click" — the control the click was aimed at is gone, so
 * the click lands on the capsule's background and nothing happens.
 *
 * A resolved-but-empty frame is deliberately NOT one of these states: `{}` is
 * truthy, so it reaches the loaded branch and `readMetricsFrame` renders its
 * em dashes there. The gap was only ever the undefined frame.
 *
 * The pending segment carries em-dash readings rather than a spinner, matching
 * the sibling usage segment's own distinction: a hang is not a fetch that is
 * about to land, and a spinner that never stops claims one.
 *
 * It is the same BUTTON in both open states, not the same width: dashes are
 * narrower than readings, and the loaded readout's width moves on its own as the
 * values do. What the continuity test below pins is that no segment mounts when
 * the frame lands — the capsule used to gain a button and a divider at that
 * moment, and a child mounting inside a `container-type`-contained group is what
 * stranded the header's backdrop.
 *
 * Refs #7967
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { safeSetItem } from '../utils/safeStorage'

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

/**
 * `api.system` is the one call under test, so it is a bare `vi.fn()` here and
 * each test installs the resolution it needs. A module-level resolved value (as
 * the sibling capsule test uses) would make the no-frame state unreachable.
 */
const systemMock = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { available: false } }),
    listApps: vi.fn().mockResolvedValue([]),
    system: (...a: unknown[]) => systemMock(...a),
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

/** Desktop width: the mobile branch renders a passive readout with no toggle. */
const setWindowWidth = (w: number) => {
  Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: w })
}

/** The metrics toggle in either OPEN state — located by its own accessible name. */
const openToggle = () => screen.getByRole('button', { name: /System metrics/ })

describe('App top bar — metrics readout with no frame', () => {
  beforeEach(() => {
    localStorage.clear()
    systemMock.mockReset()
    setWindowWidth(1400)
    // Open the readout before mount, which is the state the defect lives in:
    // the user's persisted preference says "show the readings" while the query
    // has produced neither a frame nor an error.
    safeSetItem('mc-topbar-metrics', '1')
  })

  it('renders a clickable toggle while no frame has arrived', async () => {
    // Never resolves: `sysMetrics` stays undefined and `isError` stays false for
    // the whole test, which is exactly the branch that used to push nothing.
    systemMock.mockReturnValue(new Promise(() => {}))
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByLabelText('Gateway connected')

    const toggle = openToggle()
    // aria-pressed distinguishes this from the CLOSED state's button, which
    // shares the same accessible name but reports false.
    expect(toggle.getAttribute('aria-pressed')).toBe('true')
    // Em-dash readings, not a spinner, and one per metric.
    expect(toggle.textContent).toContain('\u2014')

    // The whole point of keeping the segment: it still closes the readout, and
    // it persists that the same way the loaded toggle does, so closing from here
    // cannot leave the readout reopening on the next mount.
    fireEvent.click(toggle)
    await waitFor(() => expect(localStorage.getItem('mc-topbar-metrics')).toBe('0'))
    expect(openToggle().getAttribute('aria-pressed')).toBe('false')
  })

  it('holds one continuous toggle across the frame arriving', async () => {
    // The invariant, stated end to end: an open readout has a pressed toggle
    // before the frame and after it. Asserting continuity rather than just the
    // pending state is what rules out a fix that swaps one disappearance for
    // another — a segment that mounts on data but unmounts on the next refetch
    // would satisfy the test above and still lose the control.
    let land: (frame: unknown) => void = () => {}
    systemMock.mockReturnValue(new Promise(res => { land = res }))
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByLabelText('Gateway connected')

    expect(openToggle().getAttribute('aria-pressed')).toBe('true')
    // No reading yet, so nothing but dashes.
    expect(openToggle().textContent).not.toMatch(/\d/)

    land({ mem_used_gb: 4, mem_total_gb: 16, cpu_pct: 25, disk_total_gb: 100, disk_free_gb: 60 })

    // Same control, now carrying real numbers.
    await waitFor(() => expect(openToggle().textContent).toMatch(/\d/))
    expect(openToggle().getAttribute('aria-pressed')).toBe('true')
  })

  it('still renders the error segment when the fetch fails', async () => {
    // Regression guard on the branch that already worked: the pending state is
    // added ALONGSIDE the error state, not in place of it, so a settled failure
    // keeps saying so rather than showing indefinite em dashes.
    systemMock.mockRejectedValue(new Error('nope'))
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByLabelText('Gateway connected')

    expect(await screen.findByText(/metrics unavailable/i)).toBeTruthy()
  })
})
