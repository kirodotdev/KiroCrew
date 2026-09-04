/**
 * Focus mode — the shell's chrome (top bar + nav rail) leaves the grid and
 * becomes two edge-triggered hover overlays.
 *
 * What is worth pinning here is the part that is invisible in the markup and
 * silently wrong if it regresses: the setting BROADCASTS, so the top-bar toggle
 * and the cross-pane relay (InstancesViewport adopting a remote pane's toggle)
 * cannot disagree, and the
 * shell actually collapses its chrome TRACKS rather than merely hiding the
 * chrome — a hidden header over a 42px reserved row looks identical in a
 * screenshot and reclaims nothing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { renderWithProviders } from './helpers'
import { FOCUS_INSET, focusModeEnabled, setFocusModeEnabled, __resetFocusMode } from '../hooks/useFocusMode'
import { OVERLAY_Z_MAX, THEME_DECOR_SLOT_ID, TOPBAR_FOCUS_Z, TOPBAR_Z, useThemeDecorSlot } from '../lib/themeDecorLayer'

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))
import { isEmbeddedPane } from '../lib/embedded'

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
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
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import App from '../App'

const setWindowWidth = (w: number) => {
  Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: w })
}

const cssSource = () => {
  const here = dirname(fileURLToPath(import.meta.url))
  return readFileSync(join(here, '..', 'index.css'), 'utf8')
}

describe('focus mode — shared session state', () => {
  beforeEach(() => { __resetFocusMode(); localStorage.clear() })

  it('starts off and is never persisted', () => {
    expect(focusModeEnabled()).toBe(false)

    setFocusModeEnabled(true)
    expect(focusModeEnabled()).toBe(true)
    // Deliberately NOT persisted: focus mode is a view state that resets on
    // reload, and that is also what lets it be ONE shared value across an
    // embedded remote pane — a cross-origin iframe whose localStorage the host
    // can never reach, so a stored preference could not have been shared.
    expect(localStorage.length).toBe(0)

    setFocusModeEnabled(false)
    expect(focusModeEnabled()).toBe(false)
  })

  it('relays a pane-driven toggle up to the host, and does not echo an adopted one', () => {
    const posted: unknown[] = []
    const parent = { postMessage: (m: unknown) => { posted.push(m) } }
    Object.defineProperty(window, 'parent', { value: parent, configurable: true })
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    try {
      // A toggle the USER drove inside the pane has to reach the host, which owns
      // the shared value and re-broadcasts it to every other pane.
      setFocusModeEnabled(true)
      expect(posted).toEqual([{ type: 'mc-set-focus-mode', v: 1, on: true }])

      // Adopting the host's relayed value must NOT travel back up: that return
      // trip is what would make the two frames ping-pong.
      __resetFocusMode()
      posted.length = 0
      setFocusModeEnabled(true, { echo: false })
      expect(posted).toEqual([])
    } finally {
      vi.mocked(isEmbeddedPane).mockReturnValue(false)
      Object.defineProperty(window, 'parent', { value: window, configurable: true })
    }
  })
})

describe('focus mode — shell layout', () => {
  beforeEach(() => {
    __resetFocusMode()
    localStorage.clear()
    setWindowWidth(1400)
  })

  it('leaves the chrome docked and the peek strips unmounted when off', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    const toggle = await screen.findByTestId('focus-mode-toggle')
    expect(toggle.getAttribute('aria-pressed')).toBe('false')

    const shell = screen.getByTestId('dashboard-shell')
    // Not focus mode: the rows come from the Tailwind class and the style object
    // must not override them.
    expect(shell.style.gridTemplateRows).toBe('')
    expect(shell.style.gridTemplateColumns).toMatch(/^236px /)
    expect(screen.queryByTestId('focus-peek-top')).toBeNull()
    expect(screen.queryByTestId('focus-peek-rail')).toBeNull()
    const content = document.querySelector('[style*="grid-area: content"]') as HTMLElement
    expect(content.style.paddingLeft).toBe('')
  })

  // #7377: a theme pack's decorative overlay painted over the top bar. The shell
  // is its own stacking context (`z-[1]`), so an overlay rendered outside it
  // outranks the header at ANY z-index; the fix is a slot INSIDE the shell that
  // the overlays portal into, pinned strictly below the header in both layouts.
  it('keeps the theme decoration slot inside the shell and strictly below the header', async () => {
    expect(OVERLAY_Z_MAX).toBeLessThan(TOPBAR_Z)
    expect(OVERLAY_Z_MAX).toBeLessThan(TOPBAR_FOCUS_Z)

    let seen: HTMLElement | null = null
    function SlotProbe() { seen = useThemeDecorSlot(); return null }
    renderWithProviders(<><SlotProbe /><App /></>, { route: '/chat' })
    const toggle = await screen.findByTestId('focus-mode-toggle')

    const shell = screen.getByTestId('dashboard-shell')
    const slot = screen.getByTestId('theme-decor-slot')
    const header = document.querySelector('header.topbar') as HTMLElement
    expect(slot.id).toBe(THEME_DECOR_SLOT_ID)
    // Same stacking context as the header, and published to the layer that
    // portals into it (mounted outside the router, so it cannot take a prop).
    expect(shell.contains(slot)).toBe(true)
    expect(shell.contains(header)).toBe(true)
    expect(seen).toBe(slot)
    // Earlier in DOM order too, so even an equal z-index could not flip the win.
    expect(slot.compareDocumentPosition(header) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // Click-through, and a stacking context whose own z-index is the ceiling.
    expect(slot.className).toContain('pointer-events-none')
    expect(slot.className).toContain('fixed')
    expect(slot.style.zIndex).toBe(String(OVERLAY_Z_MAX))

    // Docked header: z from the shared constant, above the slot.
    expect(header.style.zIndex).toBe(String(TOPBAR_Z))
    // Focus mode (the absolute-overlay path that stands in for the native
    // fullscreen stacking change reported on macOS): still above the slot.
    await act(async () => { fireEvent.click(toggle) })
    expect(header.style.zIndex).toBe(String(TOPBAR_FOCUS_Z))
    expect(Number(header.style.zIndex)).toBeGreaterThan(Number(slot.style.zIndex))
  })

  it('collapses both chrome tracks and mounts the peek strips when on', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    const toggle = await screen.findByTestId('focus-mode-toggle')
    await act(async () => { fireEvent.click(toggle) })

    expect(focusModeEnabled()).toBe(true)
    expect(screen.getByTestId('focus-mode-toggle').getAttribute('aria-pressed')).toBe('true')

    const shell = screen.getByTestId('dashboard-shell')
    // The rail TRACK collapses to nothing, but the top row keeps FOCUS_INSET
    // rather than going to zero: the content row's own cards are inset 8px at the
    // bottom and zero at the top (they relied on the 42px top bar row for top
    // clearance), so a zeroed row leaves them flush against the window edge and
    // still inset below. Asserting the exact value is what stops that being
    // "fixed" back to 0 by someone reading the row as pure chrome.
    expect(shell.style.gridTemplateRows).toBe(`${FOCUS_INSET}px minmax(0,1fr)`)
    expect(shell.style.gridTemplateColumns).toMatch(/^0px /)

    // Same reasoning on the LEFT edge, which the reclaimed rail column exposed:
    // the inset lands on the content column, not on the sessions drawer, because
    // the drawer's collapse clip-path is computed against its own width and
    // padding it would desync the morph from the toggle it converges on.
    const content = document.querySelector('[style*="grid-area: content"]') as HTMLElement
    expect(content).toBeTruthy()
    expect(content.style.paddingLeft).toBe(`${FOCUS_INSET}px`)

    // Chrome stays MOUNTED and slides out of view. Unmounting the header would
    // tear down the notification and metrics popovers it owns.
    const header = document.querySelector('header.topbar') as HTMLElement
    expect(header).toBeTruthy()
    expect(header.style.transform).toBe('translateY(-100%)')
    // Hidden chrome must not eat clicks aimed at the content beneath it.
    expect(header.style.pointerEvents).toBe('none')

    const rail = screen.getByRole('navigation', { name: 'Main navigation' })
    expect(rail.style.transform).toBe('translateX(calc(-100% - 12px))')
    expect(rail.style.pointerEvents).toBe('none')

    expect(screen.getByTestId('focus-peek-top')).toBeTruthy()
    expect(screen.getByTestId('focus-peek-rail')).toBeTruthy()
  })

  it('reserves the native caption strip in focus mode, from CSS alone', () => {
    // The reserve is now pure CSS: .win-electron/.linux-electron already sit on
    // the shell root (App.tsx), so nothing has to compute a width at runtime.
    // jsdom applies no stylesheet, so the rules are pinned against index.css
    // source the same way the side-panel corner masks are.
    const css = cssSource()
    const reserve = (platform: string) => css.match(
      new RegExp(`body\\.mc-focus-mode \\.${platform}-electron \\.focus-caption-reserve\\{padding-right:(\\d+)px\\}`),
    )
    const header = (platform: string) => css.match(
      new RegExp(`\\.${platform}-electron header\\.topbar-glass\\{[\\s\\S]*?padding-right:(\\d+)px`),
    )

    for (const platform of ['win', 'linux']) {
      const rule = reserve(platform)
      expect(rule, `${platform}-electron focus-mode caption reserve`).not.toBeNull()
      // Same band the DOCKED header clears: this reserve exists only because
      // focus mode takes that header out of flow, so the two must not drift.
      expect(rule![1]).toBe(header(platform)![1])
    }

    // Deliberately NO platform-agnostic rule. It would out-specify the strip's
    // Tailwind px-2 (0,2,1 vs 0,1,0) and zero the gutter on macOS and in the
    // browser, where nothing is painted over that corner to begin with.
    expect(css).not.toContain('body.mc-focus-mode .focus-caption-reserve{')
  })

  it('hides the Electron chrome with the header and brings it back on peek', async () => {
    const setFocusModeChrome = vi.fn()
    ;(window as Window & { electronAPI?: unknown }).electronAPI = { setFocusModeChrome }
    try {
      renderWithProviders(<App />, { route: '/chat' })
      const toggle = await screen.findByTestId('focus-mode-toggle')

      // Off: nothing is claimed, so the drag bar keeps its full height and the
      // native buttons stay put.
      expect(document.body.classList.contains('mc-focus-mode')).toBe(false)
      expect(setFocusModeChrome).toHaveBeenLastCalledWith(true)

      await act(async () => { fireEvent.click(toggle) })
      // On, header hidden. BOTH signals flip together: the body class the
      // injected #electron-drag-bar rules key on, and the traffic-light bridge.
      // Left at 42px the drag region covers the content focus mode just
      // reclaimed, and a drag region is resolved before hit-testing — the top
      // band stops answering hover, including the hover that summons the header
      // back. That is unobservable in jsdom, hence pinning the handshake here.
      expect(document.body.classList.contains('mc-focus-mode')).toBe(true)
      expect(document.body.classList.contains('mc-focus-chrome')).toBe(false)
      expect(setFocusModeChrome).toHaveBeenLastCalledWith(false)

      // Peeked: the header is the drag surface the user expects, and the native
      // buttons belong on it again.
      vi.useFakeTimers()
      try {
        fireEvent.mouseEnter(screen.getByTestId('focus-peek-top'))
        act(() => { vi.advanceTimersByTime(150) })
      } finally {
        vi.useRealTimers()
      }
      expect(document.body.classList.contains('mc-focus-chrome')).toBe(true)
      expect(setFocusModeChrome).toHaveBeenLastCalledWith(true)
    } finally {
      delete (window as Window & { electronAPI?: unknown }).electronAPI
    }
  })

  it('opens the overlay when the pointer overshoots OUT of the window through its edge', async () => {
    // The edge-slam reveal is NOT gated on the desktop shell: this suite runs
    // un-mocked (`isElectron` is false — the browser case, which is also what
    // an embedded instance pane sees, since the Electron bridge does not reach
    // into its iframe). A document mouseout with relatedTarget null is a
    // genuine exit; its coordinates say which edge. In-window travel
    // (relatedTarget non-null) must not summon anything.
    renderWithProviders(<App />, { route: '/chat' })
    const toggle = await screen.findByTestId('focus-mode-toggle')
    await act(async () => { fireEvent.click(toggle) })

    const rail = screen.getByRole('navigation', { name: 'Main navigation' })
    const header = document.querySelector('header.topbar') as HTMLElement
    expect(rail.style.transform).toBe('translateX(calc(-100% - 12px))')

    // In-window travel near the edge is not an exit.
    await act(async () => {
      document.dispatchEvent(new MouseEvent('mouseout', {
        bubbles: true, relatedTarget: document.body, clientX: 4, clientY: 400,
      }))
    })
    expect(rail.style.transform).toBe('translateX(calc(-100% - 12px))')

    // Out through the LEFT edge → the rail opens immediately, no dwell.
    await act(async () => {
      document.dispatchEvent(new MouseEvent('mouseout', {
        bubbles: true, relatedTarget: null, clientX: 4, clientY: 400,
      }))
    })
    expect(rail.style.transform).toBe('translateX(0)')
    expect(header.style.transform).toBe('translateY(-100%)')
  })

  it('keeps the topbar-overlay marker on a DIRECT child of the header', async () => {
    // measureSidePanelReservedW filters only header.children for
    // data-topbar-overlay. Wrapping the ⌘K trigger (to pair it with the focus
    // toggle) without moving the marker onto the wrapper makes the centre track
    // count toward the activity panel's reserve, clamping the panel to ~25% of
    // the window. The marker must sit on the header's direct child AND that
    // child must be the cell holding the search trigger.
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('focus-mode-toggle')
    const header = document.querySelector('header.topbar-glass') as HTMLElement
    const marked = Array.from(header.children).filter(c => c.hasAttribute('data-topbar-overlay'))
    expect(marked.length).toBeGreaterThan(0)
    expect(marked.some(c => c.querySelector('[data-testid="focus-mode-toggle"]'))).toBe(true)
  })

  it('reveals the chrome when the pointer settles on a peek strip', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    const toggle = await screen.findByTestId('focus-mode-toggle')
    await act(async () => { fireEvent.click(toggle) })

    const strip = screen.getByTestId('focus-peek-rail')
    const rail = screen.getByRole('navigation', { name: 'Main navigation' })

    // Fake timers are installed HERE, not at the top of the test: testing
    // library's `findBy*` polls on real timers, so faking them before the render
    // deadlocks the query against a clock nothing advances.
    vi.useFakeTimers()
    try {
      // Hover intent: a pointer SWEEPING across the edge on its way elsewhere
      // must not summon the rail, so the reveal waits out a settle delay.
      // Advancing to just SHORT of it is what makes this assertion falsifiable —
      // merely checking the state right after mouseEnter passes for any delay at
      // all, including zero, because the open is scheduled either way.
      fireEvent.mouseEnter(strip)
      act(() => { vi.advanceTimersByTime(100) })
      expect(rail.style.transform).toBe('translateX(calc(-100% - 12px))')

      act(() => { vi.advanceTimersByTime(50) })
      expect(rail.style.transform).toBe('translateX(0)')
      expect(rail.style.pointerEvents).toBe('auto')
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps the header on screen while a header popover is open', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    const toggle = await screen.findByTestId('focus-mode-toggle')
    await act(async () => { fireEvent.click(toggle) })

    const header = document.querySelector('header.topbar') as HTMLElement
    const bell = screen.getByLabelText('Notifications')
    expect(header.style.transform).toBe('translateY(-100%)')

    // The instance switcher's menu is portaled to document.body, so the pointer
    // moving into it counts as leaving the header and the close grace would slide
    // the header out from under its own anchor. The pin is keyed on a header
    // control REPORTING an open popup, which is why the Bell exercises it here:
    // same signal, different control.
    await act(async () => { fireEvent.click(bell) })
    expect(bell.getAttribute('aria-expanded')).toBe('true')
    expect(header.style.transform).toBe('translateY(0)')
    expect(header.style.pointerEvents).toBe('auto')

    // ...and it lets go again once nothing is open, rather than latching.
    await act(async () => { fireEvent.click(bell) })
    expect(header.style.transform).toBe('translateY(-100%)')
  })

  it('does not pin the header for an inline expander that is open by default', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    const toggle = await screen.findByTestId('focus-mode-toggle')
    await act(async () => { fireEvent.click(toggle) })

    // The readout capsule's connection dot is an INLINE expand/collapse and ships
    // aria-expanded="true" from first paint with nothing popped open. An
    // aria-expanded-only signal would pin the header permanently and focus mode
    // would look broken on load — so the query also requires aria-haspopup, and
    // this is the case that proves it.
    const dot = await screen.findByLabelText(/Gateway (connected|offline)/)
    expect(dot.getAttribute('aria-expanded')).toBe('true')
    expect(dot.hasAttribute('aria-haspopup')).toBe(false)
    expect((document.querySelector('header.topbar') as HTMLElement).style.transform).toBe('translateY(-100%)')
  })

  it('gates the drop-shadow and drag classes on the chrome actually being on screen', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    const toggle = await screen.findByTestId('focus-mode-toggle')
    await act(async () => { fireEvent.click(toggle) })

    // Both surfaces stay MOUNTED and slide out of view, so a shadow that is always
    // on keeps painting its tail into the content — the header's reaches 28px past
    // its box, which is further than the 42px it slides, leaving a permanent smudge
    // across the top of the transcript. The CSS hangs the shadow (and the revealed
    // header's window-drag region) off these classes, so the class IS the contract.
    expect(document.body.classList.contains('mc-focus-mode')).toBe(true)
    expect(document.body.classList.contains('mc-focus-chrome')).toBe(false)
    expect(document.body.classList.contains('mc-focus-rail')).toBe(false)

    // The strips sit BELOW the chrome they summon, so the chrome covers them while
    // it is up. That is what lets them keep a constant size: an earlier version
    // resized them on reveal, and a hit target that changes under a resting pointer
    // flickered the chrome open/closed indefinitely (mouseleave from the shrink →
    // close grace → close → regrow under the pointer → re-open). jsdom applies no
    // CSS, so the layer is the part a test can hold.
    const header = document.querySelector('header.topbar') as HTMLElement
    expect(header.style.zIndex).toBe('62')
    expect(screen.getByTestId('focus-peek-top').className).toContain('z-[61]')
    expect(screen.getByTestId('focus-peek-rail').className).toContain('z-[61]')

    vi.useFakeTimers()
    try {
      fireEvent.mouseEnter(screen.getByTestId('focus-peek-top'))
      act(() => { vi.advanceTimersByTime(150) })
      expect(document.body.classList.contains('mc-focus-chrome')).toBe(true)

      fireEvent.mouseEnter(screen.getByTestId('focus-peek-rail'))
      act(() => { vi.advanceTimersByTime(150) })
      expect(document.body.classList.contains('mc-focus-rail')).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows the rail at full width even when the user had it collapsed', async () => {
    // A collapsed rail is 74px. As a hover-held overlay that is a hard target to
    // keep the pointer inside, so it puts itself away the moment you drift off it —
    // focus mode therefore forces it expanded. 220 is the 236px track minus the
    // rail's own 16px of horizontal margin.
    localStorage.setItem('mc-nav', '1')
    renderWithProviders(<App />, { route: '/chat' })
    const toggle = await screen.findByTestId('focus-mode-toggle')

    const rail = screen.getByRole('navigation', { name: 'Main navigation' })
    // Docked first: the preference is respected, so this is not vacuous.
    expect(rail.style.width).toBe('auto')
    const shell = screen.getByTestId('dashboard-shell')
    expect(shell.style.gridTemplateColumns).toMatch(/^74px /)

    await act(async () => { fireEvent.click(toggle) })
    expect(rail.style.width).toBe('220px')
    // ...and the preference itself is untouched, so leaving focus mode restores it.
    expect(localStorage.getItem('mc-nav')).toBe('1')

    // With no collapsed state to toggle into, the brand row's collapse control puts
    // the floating rail AWAY instead of writing the preference — otherwise it would
    // be a control that visibly does nothing while focus mode is on.
    vi.useFakeTimers()
    try {
      fireEvent.mouseEnter(screen.getByTestId('focus-peek-rail'))
      act(() => { vi.advanceTimersByTime(150) })
      expect(rail.style.transform).toBe('translateX(0)')
      act(() => { fireEvent.click(screen.getByLabelText('Collapse sidebar')) })
      expect(rail.style.transform).toBe('translateX(calc(-100% - 12px))')
    } finally {
      vi.useRealTimers()
    }
    expect(localStorage.getItem('mc-nav')).toBe('1')
  })

  it('relays its chrome visibility to the host when embedded', async () => {
    const posted: Array<Record<string, unknown>> = []
    Object.defineProperty(window, 'parent', {
      value: { postMessage: (m: Record<string, unknown>) => { posted.push(m) } },
      configurable: true,
    })
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    try {
      renderWithProviders(<App />, { route: '/chat' })
      const toggle = await screen.findByTestId('focus-mode-toggle')
      await act(async () => { fireEvent.click(toggle) })

      // A pane cannot reach the host window's chrome itself — cross-origin iframe,
      // no preload — so the native traffic lights only appear over a PANE's peeked
      // header if the pane says so and the host acts on it.
      const chrome = posted.filter(m => m.type === 'mc-focus-chrome')
      expect(chrome.at(-1)).toEqual({ type: 'mc-focus-chrome', v: 1, on: false })

      vi.useFakeTimers()
      try {
        fireEvent.mouseEnter(screen.getByTestId('focus-peek-top'))
        act(() => { vi.advanceTimersByTime(150) })
      } finally {
        vi.useRealTimers()
      }
      expect(posted.filter(m => m.type === 'mc-focus-chrome').at(-1)).toEqual({ type: 'mc-focus-chrome', v: 1, on: true })
    } finally {
      vi.mocked(isEmbeddedPane).mockReturnValue(false)
      Object.defineProperty(window, 'parent', { value: window, configurable: true })
    }
  })
})
