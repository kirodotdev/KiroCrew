/**
 * Notification Center sheet — the exit, and surviving being interrupted.
 *
 * Dismissal must keep the sheet mounted long enough to slide back out instead of
 * ripping the portal out on the same tick (the original bug: "slides in but
 * doesn't slide out on dismiss"), and — the reason the slide moved off a CSS
 * keyframe pair onto `animateDrawer` — a tap that lands DURING that exit must
 * neither re-open the sheet nor make it jump. See the two describes below for
 * the frame-level measurements behind each.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, screen, fireEvent } from '@testing-library/react'
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

const sheet = () => document.querySelector('[data-nc-phase]') as HTMLElement | null
const phase = () => sheet()?.getAttribute('data-nc-phase') ?? null

/**
 * Render, resolve the bell, THEN install fake timers.
 *
 * Order matters: `shouldAdvanceTime` lets fake time track the wall clock, so if
 * the timers were installed before the async `findByLabelText` await, a slow
 * runner could burn the 240ms unmount budget inside that await and tear the
 * portal down before the mid-exit assertions run.
 */
async function renderAndFindBell() {
  renderWithProviders(<App />, { route: '/chat' })
  const bell = await screen.findByLabelText('Notifications')
  vi.useFakeTimers({ shouldAdvanceTime: true })
  return bell
}

describe('Notification Center sheet — slide-out on dismiss', () => {
  afterEach(() => { vi.useRealTimers() })

  it('plays the exit animation before unmounting, then unmounts', async () => {
    const bell = await renderAndFindBell()

    fireEvent.click(bell)
    expect(phase()).toBe('open')

    // Dismiss: still mounted, now playing the exit animation and fully inert —
    // untouchable by pointer, keyboard and assistive tech.
    fireEvent.click(bell)
    const closingSheet = sheet()
    expect(closingSheet).toBeTruthy()
    expect(closingSheet!.getAttribute('data-nc-phase')).toBe('closing')
    expect(closingSheet!.classList.contains('pointer-events-none')).toBe(true)
    expect(closingSheet!.hasAttribute('inert')).toBe(true)
    expect(closingSheet!.getAttribute('aria-hidden')).toBe('true')

    // ...and gone once the settle has reported arrival.
    await act(async () => { vi.advanceTimersByTime(1200) })
    expect(sheet()).toBeNull()
  })

  /**
   * REGRESSION — a tap during the exit must not re-open the sheet.
   *
   * This test asserted the OPPOSITE ("re-opening mid-exit cancels the pending
   * unmount"), and that behaviour was the reported bug: dismissal set
   * `closing = true` AND `open = false` in one commit while the sheet stayed on
   * screen for the whole exit, so for those 240ms the bell's `if (open)` toggle
   * read a tap as "it's closed" and re-entered the sheet. On a 390px phone the
   * re-entry flung the sheet the full 410px offscreen and replayed the entire
   * 420ms entrance — measured frame-to-frame, tx 9.97 -> 410.00 in one frame.
   * An impatient double-tap-to-dismiss therefore left the panel OPEN and visibly
   * re-animated, and needed a third tap to actually close.
   *
   * The single `phase` value is what makes that unrepresentable: anything other
   * than `closed` means the sheet is on screen, so the tap belongs to the
   * dismissal already in flight.
   */
  it('ignores a tap during the exit instead of re-opening', async () => {
    const bell = await renderAndFindBell()

    fireEvent.click(bell)
    fireEvent.click(bell)
    expect(phase()).toBe('closing')

    fireEvent.click(bell)
    expect(phase(), 'a tap mid-exit must not re-enter the sheet').toBe('closing')
    // The dismissal still completes on its own — the tap neither resurrects the
    // sheet nor strands it half-open.
    await act(async () => { vi.advanceTimersByTime(1200) })
    expect(sheet()).toBeNull()

    // And the bell is live again immediately after, so refusing the mid-exit tap
    // costs responsiveness only for the length of the exit.
    fireEvent.click(bell)
    expect(phase()).toBe('open')
    expect(sheet()!.hasAttribute('inert')).toBe(false)
    expect(sheet()!.hasAttribute('aria-hidden')).toBe(false)
  })

  it('returns focus to the bell when Escape dismisses the sheet', async () => {
    const bell = await renderAndFindBell()

    fireEvent.click(bell)
    expect(sheet()).toBeTruthy()

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(document.activeElement).toBe(bell)
    expect(phase()).toBe('closing')
  })
})

/**
 * The slide must be INTERRUPTIBLE, and that is a property of where each
 * animation STARTS.
 *
 * The sheet used to slide on a pair of Tailwind keyframes whose `from` was a
 * hardcoded endpoint (`translateX(calc(100% + 20px))` entering,
 * `translateX(0)` leaving). CSS has no "resume from the current transform", so
 * swapping the class mid-flight teleported the sheet to the incoming animation's
 * origin. Measured in Chromium on a 390px sheet: dismissing 100ms into the
 * entrance moved it tx 99.88 -> 0.00 in ONE frame (the remaining ~99px to
 * fully-open, ~325px if interrupted at 30ms) and only then slid out; re-opening
 * 50ms into the exit moved it tx 9.97 -> 410.00 in one frame and replayed the
 * whole 420ms entrance. Both read to a user as the panel opening a second time.
 *
 * `animateDrawer` keyframes from the offset the OUTGOING animation is
 * presenting, so the test that matters is that a reversal's first keyframe is
 * the live position rather than a fixed endpoint.
 */
describe('Notification Center sheet — interruptible slide', () => {
  it('keyframes a reversal from the live offset, not from a fixed endpoint', async () => {
    const calls: { keyframes: Record<string, string>[]; timing: Record<string, unknown> }[] = []
    const proto = HTMLElement.prototype as unknown as { animate?: unknown }
    const hadAnimate = 'animate' in proto
    const prevAnimate = proto.animate
    proto.animate = function (keyframes: Record<string, string>[], timing: Record<string, unknown>) {
      calls.push({ keyframes, timing })
      return { cancel() {}, set onfinish(_v: unknown) {}, set oncancel(_v: unknown) {} }
    }
    try {
      const bell = await renderAndFindBell()

      // Entrance: nothing is running yet, so it legitimately starts parked —
      // 400px desktop sheet + the 20px its shadow needs to clear the edge.
      //
      // The tick matters: the settle is kicked off in the SAME tick as the
      // setState that mounts the sheet, so there is nothing to animate yet and
      // `animateDrawer` waits a bounded number of frames for the element to
      // appear. Without advancing here it would degrade to the main-thread
      // fallback and record nothing.
      fireEvent.click(bell)
      await act(async () => { vi.advanceTimersByTime(50) })
      expect(calls.length, 'the entrance must reach the compositor').toBeGreaterThan(0)
      const entrance = calls[calls.length - 1]
      // A layout property here is the jank class this whole path exists to
      // avoid — one reflow of the sheet AND its subtree per frame.
      for (const frame of entrance.keyframes) {
        expect(Object.keys(frame), 'the settle may animate transform ONLY').toEqual(['transform'])
      }
      expect(entrance.keyframes[0].transform).toBe('translate3d(420px, 0, 0)')
      expect(entrance.keyframes[1].transform).toBe('translate3d(0px, 0, 0)')
      // The 420 above is NC_SHEET_DESKTOP_W + NC_SHEET_CLEARANCE, and Tailwind
      // cannot take an interpolated class, so the rendered width is a second
      // spelling of that constant. Pinned together here: a parked offset that
      // disagrees with the width either leaves a strip on screen before the
      // entrance starts, or spends the 420ms crossing space the sheet never
      // occupies.
      expect(sheet()!.classList.contains('w-[400px]')).toBe(true)

      // Now stand where a half-played entrance would have the sheet. A matrix,
      // because that is the form a resolved transform is read back in.
      const el = sheet()!
      el.style.transform = 'matrix(1, 0, 0, 1, 120, 0)'

      // Reverse. The exit must pick the sheet up at 120, NOT at the old
      // `nc-slide-out` origin of 0 — starting at 0 IS the one-frame snap to
      // fully-open that the measurements above recorded.
      fireEvent.click(bell)
      const exit = calls[calls.length - 1]
      expect(exit, 'the exit must reach the compositor too').not.toBe(entrance)
      expect(exit.keyframes[0].transform, 'a reversal must continue from the live offset').toBe('translate3d(120px, 0, 0)')
      expect(exit.keyframes[1].transform).toBe('translate3d(420px, 0, 0)')
    } finally {
      if (hadAnimate) proto.animate = prevAnimate
      else delete proto.animate
    }
  })
})
