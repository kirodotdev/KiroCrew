/**
 * useKeyboardViewportReset — after the iOS keyboard hides, the page must be
 * scrolled back to 0,0 (only while the dashboard shell is mounted).
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useKeyboardViewportReset } from './useKeyboardViewportReset'

type Listener = () => void

function installVisualViewport(height: number) {
  const listeners: Record<string, Listener[]> = {}
  const vv = {
    height,
    width: 390,
    offsetTop: 0,
    addEventListener: (t: string, l: Listener) => { (listeners[t] ||= []).push(l) },
    removeEventListener: (t: string, l: Listener) => { listeners[t] = (listeners[t] || []).filter((x) => x !== l) },
  }
  Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv })
  return { vv, fire: (t: string) => (listeners[t] || []).forEach((l) => l()) }
}

function setLayoutWidth(width: number) {
  Object.defineProperty(document.documentElement, 'clientWidth', { configurable: true, value: width })
}

function setInnerHeight(height: number) {
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: height })
}

describe('useKeyboardViewportReset', () => {
  let scrollTo: ReturnType<typeof vi.fn>
  // Save and restore every global this file overrides, so nothing leaks into a
  // test added here later (cf. ChatInput.plusMenuKeyboard.test.tsx).
  let originalViewport: PropertyDescriptor | undefined
  let originalInnerHeight: PropertyDescriptor | undefined
  let originalScrollY: PropertyDescriptor | undefined
  let originalScrollTo: typeof window.scrollTo
  let originalClientWidth: PropertyDescriptor | undefined

  beforeEach(() => {
    vi.useFakeTimers()
    originalViewport = Object.getOwnPropertyDescriptor(window, 'visualViewport')
    originalInnerHeight = Object.getOwnPropertyDescriptor(window, 'innerHeight')
    originalScrollY = Object.getOwnPropertyDescriptor(window, 'scrollY')
    originalScrollTo = window.scrollTo
    originalClientWidth = Object.getOwnPropertyDescriptor(document.documentElement, 'clientWidth')
    setLayoutWidth(390)
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 800 })
    scrollTo = vi.fn()
    window.scrollTo = scrollTo as unknown as typeof window.scrollTo
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 40 })
    const shell = document.createElement('div')
    shell.dataset.testid = 'dashboard-shell'
    document.body.replaceChildren(shell)
  })
  afterEach(() => {
    vi.useRealTimers()
    document.body.replaceChildren()
    if (originalViewport) Object.defineProperty(window, 'visualViewport', originalViewport)
    else delete (window as { visualViewport?: VisualViewport }).visualViewport
    if (originalInnerHeight) Object.defineProperty(window, 'innerHeight', originalInnerHeight)
    if (originalScrollY) Object.defineProperty(window, 'scrollY', originalScrollY)
    window.scrollTo = originalScrollTo
    if (originalClientWidth) Object.defineProperty(document.documentElement, 'clientWidth', originalClientWidth)
    else delete (document.documentElement as { clientWidth?: number }).clientWidth
  })

  it('resets scroll once the keyboard closes', () => {
    const { vv, fire } = installVisualViewport(500) // keyboard open
    renderHook(() => useKeyboardViewportReset())
    fire('resize')
    vi.advanceTimersByTime(1000)
    expect(scrollTo).not.toHaveBeenCalled() // never fight the pan while open

    vv.height = 800 // keyboard closed
    fire('resize')
    vi.advanceTimersByTime(1000)
    expect(scrollTo).toHaveBeenCalledWith(0, 0)
  })

  it('does nothing while the keyboard is open, even on focusout', () => {
    installVisualViewport(500)
    renderHook(() => useKeyboardViewportReset())
    document.dispatchEvent(new Event('focusout'))
    vi.advanceTimersByTime(1000)
    expect(scrollTo).not.toHaveBeenCalled()
  })

  it('does not cancel the focus pan: a window scroll that precedes the viewport shrink is a no-op', () => {
    // The race Opus flagged: iOS pans to reveal the composer and fires a window
    // scroll BEFORE visualViewport reports the reduced height. A synchronous
    // reset would read keyboardOpen() === false (height not shrunk yet) and
    // cancel the pan. Deferring re-checks after settle, by when the keyboard is
    // reported open — so no reset.
    const vp = installVisualViewport(800) // keyboard not yet reported
    renderHook(() => useKeyboardViewportReset())
    window.dispatchEvent(new Event('scroll')) // pan scroll arrives first
    vp.vv.height = 500 // keyboard now reported open, before the settle timers run
    vi.advanceTimersByTime(1000)
    expect(scrollTo).not.toHaveBeenCalled()
  })

  it('undoes a stale pan: a window scroll after the keyboard is gone resets', () => {
    installVisualViewport(800) // keyboard closed throughout
    renderHook(() => useKeyboardViewportReset())
    window.dispatchEvent(new Event('scroll'))
    vi.advanceTimersByTime(1000)
    expect(scrollTo).toHaveBeenCalledWith(0, 0)
  })

  it('keeps the composer pan on iPhone, where innerHeight shrinks with the keyboard', () => {
    // iPhone Safari reports the keyboard-reduced height in window.innerHeight
    // as well as visualViewport.height, so their difference stays ~0 while the
    // keyboard is up. Reading that as "closed" scrolled the page back to 0,0
    // and left the composer under the keyboard.
    const { vv, fire } = installVisualViewport(800)
    renderHook(() => useKeyboardViewportReset())
    vv.height = 450 // keyboard opens...
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 450 }) // ...and innerHeight follows
    fire('resize')
    window.dispatchEvent(new Event('scroll')) // Safari pans to reveal the composer
    fire('scroll')
    vi.advanceTimersByTime(1000)
    expect(scrollTo).not.toHaveBeenCalled()

    vv.height = 800 // keyboard closed: the stale pan is still undone
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 800 })
    fire('resize')
    vi.advanceTimersByTime(1000)
    expect(scrollTo).toHaveBeenCalledWith(0, 0)
  })

  it('leaves a pinch-zoomed page where it is', () => {
    // Pinch-zoom shrinks the visual viewport (and on iOS innerWidth and
    // innerHeight with it) but not the layout width. Keying the height record
    // on the visual width restarted it at the zoomed height, read the zoom as
    // "keyboard closed" and snapped the zoomed reader back to 0,0 on every pan.
    const { vv, fire } = installVisualViewport(800)
    renderHook(() => useKeyboardViewportReset())
    vv.width = 195
    vv.height = 400
    setInnerHeight(400)
    fire('resize')
    vv.offsetTop = 120 // the reader pans while zoomed
    fire('scroll')
    vi.advanceTimersByTime(1000)
    expect(scrollTo).not.toHaveBeenCalled()

    vv.width = 390 // zoomed back out: a stale pan is still undone
    vv.height = 800
    vv.offsetTop = 0
    setInnerHeight(800)
    fire('resize')
    vi.advanceTimersByTime(1000)
    expect(scrollTo).toHaveBeenCalledWith(0, 0)
  })

  it('keeps the composer pan when the phone rotates while typing', () => {
    const { vv, fire } = installVisualViewport(800)
    const composer = document.createElement('textarea')
    document.body.append(composer)
    renderHook(() => useKeyboardViewportReset())
    composer.focus()
    vv.height = 450 // portrait keyboard opens
    setInnerHeight(450)
    fire('resize')

    setLayoutWidth(844) // rotate to landscape with the keyboard still up:
    vv.width = 844 // the new width's record starts at the keyboard-reduced height
    vv.height = 200
    setInnerHeight(200)
    fire('resize')
    window.dispatchEvent(new Event('scroll'))
    vi.advanceTimersByTime(1000)
    expect(scrollTo).not.toHaveBeenCalled()

    composer.blur() // typing ends and the keyboard closes
    document.dispatchEvent(new Event('focusout'))
    vv.height = 390
    setInnerHeight(390)
    fire('resize')
    vi.advanceTimersByTime(1000)
    expect(scrollTo).toHaveBeenCalledWith(0, 0)
  })

  it('leaves document scroll alone on routes without the dashboard shell', () => {
    const { vv, fire } = installVisualViewport(500)
    document.body.replaceChildren()
    renderHook(() => useKeyboardViewportReset())
    fire('resize')
    vv.height = 800
    fire('resize')
    vi.advanceTimersByTime(1000)
    expect(scrollTo).not.toHaveBeenCalled()
  })

  it('removes its listeners on unmount', () => {
    const { vv, fire } = installVisualViewport(500)
    const { unmount } = renderHook(() => useKeyboardViewportReset())
    fire('resize')
    unmount()
    vv.height = 800
    fire('resize')
    vi.advanceTimersByTime(1000)
    expect(scrollTo).not.toHaveBeenCalled()
  })
})
