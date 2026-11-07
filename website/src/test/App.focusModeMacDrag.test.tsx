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
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { __resetFocusMode } from '../hooks/useFocusMode'
import { computeTitleBandDragGaps } from '../lib/dragGaps'

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
// The real image viewer: App renders it in place, ahead of the drag strips.
vi.mock('../components/MarkdownRenderer', async () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
  Lightbox: (await import('../components/markdown/Lightbox')).Lightbox,
}))
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

  it('mounts a drag surface whenever the header is on screen, and only then', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    const toggle = await screen.findByTestId('focus-mode-toggle')

    // Docked: strips too. The injected drag bar alone is cancelled by any later
    // no-drag rectangle reaching into the band (a theme's decoration iframes,
    // transcript content scrolled up under the glass header), so the header's
    // gaps get strips painted after all of them.
    const docked = screen.getAllByTestId('header-drag-strip')
    expect(docked.length).toBeGreaterThan(0)
    expect(screen.getByTestId('dashboard-shell').contains(docked[0])).toBe(false)

    // Focus on but header hidden: nothing to drag, and a strip left mounted here
    // would be a 42px drag region over the content focus mode just reclaimed.
    await act(async () => { fireEvent.click(toggle) })
    expect(screen.queryByTestId('header-drag-strip')).toBeNull()

    // Peeked: the strip mounts. The mount is the region toggle — macOS ignores an
    // in-place `-webkit-app-region` flip on the transformed header, so the drag
    // surface has to enter the layout fresh each time the header appears.
    vi.useFakeTimers()
    try {
      fireEvent.mouseEnter(screen.getByTestId('focus-peek-top'))
      act(() => { vi.advanceTimersByTime(150) })

      const strip = screen.getByTestId('header-drag-strip')
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
    expect(screen.queryByTestId('header-drag-strip')).toBeNull()
  })

  it('re-measures the docked strips when a header control moves', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('focus-mode-toggle')
    const header = document.querySelector('header.topbar-glass') as HTMLElement
    // jsdom lays nothing out, so every control has a zero box and the whole band
    // is one gap. Give one control a real box AFTER mount: a strip measured once
    // and never again would still span it and turn its clicks into window drags.
    expect(screen.getAllByTestId('header-drag-strip')).toHaveLength(1)
    const control = header.querySelector('button') as HTMLElement
    control.getBoundingClientRect = () => ({ left: 600, right: 700, top: 7, bottom: 35, width: 100, height: 28, x: 600, y: 7, toJSON: () => ({}) })
    await act(async () => { header.setAttribute('class', `${header.className} tb-reflow`) })
    const strips = screen.getAllByTestId('header-drag-strip')
    expect(strips).toHaveLength(2)
    // Neither strip may reach into the control's box (padded by CONTROL_PAD_PX).
    for (const s of strips) {
      const left = parseFloat(s.style.left)
      const right = left + parseFloat(s.style.width)
      expect(right <= 600 || left >= 700).toBe(true)
    }
  })

  // Electron unions app-region rectangles in document order, and the strips
  // come after every surface rendered in place in the app root. Over one drawn
  // across the band they would turn its controls' clicks into window drags, so
  // the gaps avoid every visible control in the band, wherever it lives, and
  // re-measure (one animation frame later) when such a surface opens or closes.
  // The rAF clock alone is faked, so React and the queries keep real timers.
  describe('controls of other surfaces in the band', () => {
    const box = (el: Element, left: number, right: number, top = 8, bottom = 36) => {
      (el as HTMLElement).getBoundingClientRect = () => ({ left, right, top, bottom, width: right - left, height: bottom - top, x: left, y: top, toJSON: () => ({}) })
    }
    const expectStripsClear = (left: number, right: number) => {
      const strips = screen.getAllByTestId('header-drag-strip')
      for (const s of strips) {
        const l = parseFloat(s.style.left)
        const r = l + parseFloat(s.style.width)
        expect(r <= left || l >= right).toBe(true)
      }
      return strips
    }
    const expectOneFullStrip = () => {
      const strips = screen.getAllByTestId('header-drag-strip')
      expect(strips).toHaveLength(1)
      expect(parseFloat(strips[0].style.width)).toBe(1400)
    }
    const nextFrame = () => act(() => { vi.advanceTimersToNextFrame() })
    afterEach(() => { vi.useRealTimers() })

    it('keeps the strips off the image viewer toolbar while it is open, and spans the band again after', async () => {
      renderWithProviders(<App />, { route: '/chat' })
      await screen.findByTestId('focus-mode-toggle')
      expectOneFullStrip()
      vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame'] })

      await act(async () => { window.dispatchEvent(new CustomEvent('lightbox', { detail: { src: '/a.png', alt: 'a' } })) })
      const button = screen.getByTestId('lightbox-toolbar').querySelector('button') as HTMLElement
      box(button, 1300, 1380, 16, 44)
      nextFrame()
      expect(expectStripsClear(1300, 1380).length).toBeGreaterThan(0)

      await act(async () => { fireEvent.keyDown(window, { key: 'Escape' }) })
      expect(screen.queryByTestId('lightbox-toolbar')).toBeNull()
      nextFrame()
      expectOneFullStrip()
    })

    it('keeps the strips off the phone nav drawer while it is open, and spans the band again after', async () => {
      // A fresh function, so useIsMobile re-reads the media query as narrow.
      const desktopMatchMedia = window.matchMedia
      window.matchMedia = ((query: string) => ({
        matches: query.startsWith('(max-width'),
        addEventListener: () => {},
        removeEventListener: () => {},
      })) as unknown as typeof window.matchMedia
      try {
        // Off /chat: there the chat page's own bar carries the menu button.
        renderWithProviders(<App />, { route: '/schedule' })
        const menu = (await screen.findAllByRole('button', { name: 'Open menu' }))[0]
        expectOneFullStrip()
        vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame'] })

        await act(async () => { fireEvent.click(menu) })
        const drawer = document.querySelector('nav[data-covers-pane]') as HTMLElement
        // The drawer's full-width brand toggle reaches into the band.
        box(drawer.querySelector('button') as HTMLElement, 8, 300, 17, 45)
        nextFrame()
        expect(expectStripsClear(8, 300).length).toBeGreaterThan(0)

        await act(async () => { fireEvent.click(menu) })
        expect(document.querySelector('[data-covers-pane]')).toBeNull()
        // Still mounted through its slide-out: off the band once it has left.
        box(drawer.querySelector('button') as HTMLElement, 0, 0, 0, 0)
        await act(async () => { drawer.setAttribute('style', 'transform: translateX(-110%)') })
        nextFrame()
        expectOneFullStrip()
      } finally {
        window.matchMedia = desktopMatchMedia
      }
    })

    it('covers transcript controls scrolled up past the scroller, but not one in a fixed layer', async () => {
      renderWithProviders(<App />, { route: '/chat' })
      await screen.findByTestId('focus-mode-toggle')
      const main = document.getElementById('main-content') as HTMLElement
      box(main, 0, 1400, 42, 900)
      vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame'] })

      // Clipped out of sight above the scroller: exactly what the strips cover.
      const scrolled = document.createElement('button')
      box(scrolled, 600, 700, 4, 30)
      await act(async () => { main.appendChild(scrolled) })
      nextFrame()
      expectOneFullStrip()

      // Across the scroller's top edge (a tall disclosure card): the part in the
      // band is clipped, so it is covered too.
      const across = document.createElement('div')
      across.tabIndex = 0
      box(across, 0, 1400, 20, 300)
      await act(async () => { main.appendChild(across) })
      nextFrame()
      expectOneFullStrip()
      await act(async () => { across.remove() })

      // In a fixed layer inside the scroller (an expanded widget): not clipped.
      const layer = document.createElement('div')
      layer.style.position = 'fixed'
      const pinned = document.createElement('button')
      box(pinned, 600, 700, 16, 40)
      layer.appendChild(pinned)
      await act(async () => { main.appendChild(layer) })
      nextFrame()
      expect(expectStripsClear(600, 700)).toHaveLength(2)
    })

    // Peeked in focus mode the opaque header overlays the content column, so a
    // column control under it (the chat title overlay's buttons) cannot be
    // clicked and must not cut the header's strips. One outside the column
    // still does.
    it('lets the peeked header cover content-column controls under it', async () => {
      renderWithProviders(<App />, { route: '/chat' })
      const toggle = await screen.findByTestId('focus-mode-toggle')
      await act(async () => { fireEvent.click(toggle) })
      vi.useFakeTimers()
      try {
        fireEvent.mouseEnter(screen.getByTestId('focus-peek-top'))
        act(() => { vi.advanceTimersByTime(150) })
        const main = document.getElementById('main-content') as HTMLElement
        box(main, 0, 1400, 0, 900)
        const under = document.createElement('button')
        box(under, 600, 700, 4, 30)
        await act(async () => { main.appendChild(under) })
        act(() => { vi.advanceTimersToNextFrame() })
        expectOneFullStrip()

        const drawer = document.createElement('button')
        box(drawer, 1000, 1100, 4, 30)
        await act(async () => { document.body.appendChild(drawer) })
        act(() => { vi.advanceTimersToNextFrame() })
        expect(expectStripsClear(1000, 1100)).toHaveLength(2)
        drawer.remove()
      } finally {
        vi.useRealTimers()
      }
    })

    // In focus mode the shell's grid gives #main-content an 8px top row, so the
    // landmark (tabIndex={-1}, matched by `[tabindex]`) reaches into the band.
    // It is a focus container, not a control: counted, its column-wide box
    // would block the band and the peeked header would lose every strip. The
    // same holds for the transcript scroller and any wrapper of the header or
    // the landmark; their own controls still count.
    it('keeps the strips when containers and focus targets reach into the band', async () => {
      renderWithProviders(<App />, { route: '/chat' })
      await screen.findByTestId('focus-mode-toggle')
      const main = document.getElementById('main-content') as HTMLElement
      vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame'] })

      box(main, 0, 1400, 8, 900)
      // A focusable wrapper of the landmark: the shell, not a control.
      const wrapper = main.parentElement as HTMLElement
      wrapper.setAttribute('tabindex', '0')
      box(wrapper, 0, 1400, 0, 900)
      const scroller = document.createElement('div')
      scroller.tabIndex = -1
      box(scroller, 0, 1400, 8, 900)
      // A role-less focus target with no controls inside (a clickable row, an
      // iframe) may be a click target itself, so it still counts.
      const frame = document.createElement('iframe')
      frame.tabIndex = -1
      box(frame, 200, 300, 12, 40)
      // A real control inside them, unclipped in the band: it still counts.
      const control = document.createElement('button')
      box(control, 600, 700, 12, 40)
      // A role-bearing focus container (a dialog) still counts as a whole, not
      // just through the controls inside it.
      const dialog = document.createElement('div')
      dialog.setAttribute('role', 'dialog')
      dialog.tabIndex = -1
      box(dialog, 1000, 1100, 12, 40)
      dialog.appendChild(document.createElement('button'))
      scroller.append(frame, control, dialog)
      await act(async () => { main.appendChild(scroller) })
      nextFrame()
      const strips = expectStripsClear(600, 700)
      expectStripsClear(1000, 1100)
      expectStripsClear(200, 300)
      expect(strips).toHaveLength(4)

      wrapper.removeAttribute('tabindex')
    })

    // A floating panel (the live desktop view) is moved by a plain <header> and
    // resized by role="separator" grips, none of them a control. Pushed up into
    // the band, the panel and a grip of it still cut the strips.
    it('keeps the strips off a floating panel and its resize grips in the band', async () => {
      renderWithProviders(<App />, { route: '/chat' })
      await screen.findByTestId('focus-mode-toggle')
      vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame'] })

      const panel = document.createElement('div')
      panel.setAttribute('role', 'dialog')
      panel.style.position = 'fixed'
      box(panel, 900, 1100, 16, 400)
      const grip = document.createElement('div')
      grip.setAttribute('role', 'separator')
      box(grip, 300, 340, 10, 20)
      await act(async () => { document.body.append(panel, grip) })
      nextFrame()
      expectStripsClear(900, 1100)
      expect(expectStripsClear(300, 340)).toHaveLength(3)
      panel.remove()
      grip.remove()
    })

    // In the app every ancestor of the header also holds #main-content, so a
    // header-only wrapper needs a document of its own (and one with no landmark).
    it('does not count a focusable wrapper of the header as a control', () => {
      const doc = document.implementation.createHTMLDocument('')
      const wrap = doc.createElement('div')
      wrap.tabIndex = 0
      box(wrap, 0, 1400, 0, 42)
      const header = doc.createElement('header')
      const control = doc.createElement('button')
      box(control, 600, 700, 8, 36)
      header.appendChild(control)
      wrap.appendChild(header)
      doc.body.appendChild(wrap)
      const gaps = computeTitleBandDragGaps(header, 1400, doc)
      expect(gaps).toEqual([{ x: 0, w: 594 }, { x: 706, w: 694 }])
    })
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

    const rail = document.querySelector('nav.focus-chrome-rail') as HTMLElement
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
