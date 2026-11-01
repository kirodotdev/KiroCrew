/**
 * useComposerKeyboardLift — the composer is lifted above an on-screen keyboard
 * by the amount its bottom edge is hidden below the visible band, and by NOTHING
 * when the band already reaches it (desktop at rest, and iPhone where Safari's
 * pan already cleared the composer — no double lift).
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useRef } from 'react'
import { useComposerKeyboardLift } from './useComposerKeyboardLift'

type Listener = () => void

function installVisualViewport(opts: { height: number; offsetTop: number; scale?: number }) {
  const listeners: Record<string, Listener[]> = {}
  const vv = {
    height: opts.height,
    width: 820,
    offsetTop: opts.offsetTop,
    scale: opts.scale ?? 1,
    addEventListener: (t: string, l: Listener) => { (listeners[t] ||= []).push(l) },
    removeEventListener: (t: string, l: Listener) => { listeners[t] = (listeners[t] || []).filter((x) => x !== l) },
  }
  Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv })
  return { vv, fire: (t: string) => (listeners[t] || []).forEach((l) => l()) }
}

/** A div whose getBoundingClientRect().bottom is a fixed value. */
function elementAtBottom(bottom: number): HTMLDivElement {
  const el = document.createElement('div')
  el.getBoundingClientRect = () => ({ bottom, top: bottom - 80, left: 0, right: 820, width: 820, height: 80, x: 0, y: bottom - 80, toJSON: () => ({}) }) as DOMRect
  return el
}

/**
 * A div that behaves like the real composer: its UNTRANSFORMED bottom is fixed,
 * but the hook's returned lift is applied as `translateY(-lift)`, so the
 * MEASURED bottom is the untransformed one minus whatever lift is applied.
 * `applied` is updated by the test to mirror the transform the component paints.
 */
function transformedElement(untransformedBottom: number, appliedRef: { current: number }): HTMLDivElement {
  const el = document.createElement('div')
  el.getBoundingClientRect = () => {
    const bottom = untransformedBottom - appliedRef.current
    return ({ bottom, top: bottom - 80, left: 0, right: 820, width: 820, height: 80, x: 0, y: bottom - 80, toJSON: () => ({}) }) as DOMRect
  }
  return el
}

/** Mount the hook with a ref pre-pointed at `el`. */
function renderLift(el: HTMLElement | null) {
  return renderHook(() => {
    const ref = useRef<HTMLElement>(el)
    return useComposerKeyboardLift(ref)
  })
}

describe('useComposerKeyboardLift', () => {
  let originalViewport: PropertyDescriptor | undefined
  let originalInnerHeight: PropertyDescriptor | undefined
  let originalRaf: typeof window.requestAnimationFrame
  let originalCaf: typeof window.cancelAnimationFrame

  beforeEach(() => {
    originalViewport = Object.getOwnPropertyDescriptor(window, 'visualViewport')
    originalInnerHeight = Object.getOwnPropertyDescriptor(window, 'innerHeight')
    originalRaf = window.requestAnimationFrame
    originalCaf = window.cancelAnimationFrame
    // Run the scheduled measure synchronously so the first render settles.
    window.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 1 }) as typeof window.requestAnimationFrame
    window.cancelAnimationFrame = (() => {}) as typeof window.cancelAnimationFrame
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 1024 })
  })
  afterEach(() => {
    if (originalViewport) Object.defineProperty(window, 'visualViewport', originalViewport)
    else delete (window as { visualViewport?: VisualViewport }).visualViewport
    if (originalInnerHeight) Object.defineProperty(window, 'innerHeight', originalInnerHeight)
    window.requestAnimationFrame = originalRaf
    window.cancelAnimationFrame = originalCaf
    vi.restoreAllMocks()
  })

  it('is 0 at rest: the visible band reaches the composer bottom (desktop no-op)', () => {
    // No keyboard: band is the full layout viewport, composer sits inside it.
    installVisualViewport({ height: 1024, offsetTop: 0 })
    const { result } = renderLift(elementAtBottom(1000))
    expect(result.current).toBe(0)
  })

  it('is 0 when a pan already cleared the composer (iPhone, no double lift)', () => {
    // Keyboard up: visible band is offsetTop..offsetTop+height. Safari panned the
    // page so the composer bottom (640) sits exactly at the band bottom.
    installVisualViewport({ height: 600, offsetTop: 40 }) // band bottom = 640
    const { result } = renderLift(elementAtBottom(640))
    expect(result.current).toBe(0)
  })

  it('lifts by the overlap when the keyboard still covers the composer (iPad)', () => {
    // Keyboard up, no sufficient pan: band bottom = 0 + 600 = 600, but the
    // composer bottom is at 1000 (full layout height), 400px behind the keyboard.
    installVisualViewport({ height: 600, offsetTop: 0 })
    const { result } = renderLift(elementAtBottom(1000))
    expect(result.current).toBe(400)
  })

  it('does NOT lift for a desktop pinch-zoom, which also shrinks the visual viewport (scale != 1)', () => {
    // 2x trackpad pinch-zoom on desktop: vv.height shrinks to ~innerHeight/scale
    // and the origin pans, so the band is reduced well past the keyboard
    // threshold — but there is no keyboard. vv.scale != 1 is the tell.
    installVisualViewport({ height: 512, offsetTop: 100, scale: 2 }) // band bottom 612, reduced 412
    const { result } = renderLift(elementAtBottom(1000)) // would be +388 if treated as a keyboard
    expect(result.current).toBe(0)
  })

  it('does NOT lift for a sub-threshold shrink such as a collapsing URL bar', () => {
    // Band reduced by only 80px (< 120 threshold): a collapsing Safari URL bar,
    // not a keyboard. No lift.
    installVisualViewport({ height: 944, offsetTop: 0 }) // innerHeight 1024, reduced 80
    const { result } = renderLift(elementAtBottom(1000))
    expect(result.current).toBe(0)
  })

  it('still lifts the remaining overlap when Safari has panned (offsetTop raised, keyboard open)', () => {
    // iPad, keyboard open at scale 1: Safari pans to reveal the textarea, so
    // offsetTop is raised to 324 while vv.height stays 600 (innerHeight 1024).
    // The keyboard-open test must use the viewport-height reduction
    // (1024-600=424 > 120) and NOT innerHeight-(offsetTop+height)=100, which
    // would wrongly read the keyboard as closed. Remaining overlap is the
    // composer bottom (1000) minus the visible-band bottom (324+600=924) = 76.
    installVisualViewport({ height: 600, offsetTop: 324 })
    const { result } = renderLift(elementAtBottom(1000))
    expect(result.current).toBe(76)
  })

  it('keeps the SAME lift when it re-measures the already-transformed element (no flicker)', () => {
    // Regression: the hook measures the element it also lifts via translateY.
    // Without adding the applied lift back, the second measure reads the LIFTED
    // rect (bottom now inside the band), computes overlap 0, and collapses the
    // lift — the composer flickers between lifted and hidden. The measurement
    // must be idempotent across re-measures.
    const { fire } = installVisualViewport({ height: 600, offsetTop: 0 }) // band bottom 600
    const applied = { current: 0 }
    const el = transformedElement(1000, applied) // untransformed bottom 1000 -> overlap 400
    const { result } = renderLift(el)
    expect(result.current).toBe(400)

    // The component now paints translateY(-400); the element's measured bottom
    // becomes 1000 - 400 = 600 (exactly at the band bottom, as if cleared).
    applied.current = result.current
    // A visual-viewport resize / scroll (or a transcript scroll) re-measures.
    act(() => fire('resize'))
    // Must still be 400, not 0: the lift held instead of collapsing.
    expect(result.current).toBe(400)

    // And once more, to prove it is stable rather than oscillating.
    applied.current = result.current
    act(() => fire('scroll'))
    expect(result.current).toBe(400)
  })

  it('is 0 when visualViewport is unavailable (no-op, not a hazard)', () => {
    delete (window as { visualViewport?: VisualViewport }).visualViewport
    const { result } = renderLift(elementAtBottom(1000))
    expect(result.current).toBe(0)
  })
})
