/**
 * Where a zoomed target may be panned to: each edge as far as the area it is
 * seen in. That area is its nearest clipping ancestor below the overlay, or the
 * screen. A clipping pane ABOVE a `position: fixed` overlay does not clip the
 * overlay, so it must not bound the pan: the app-detail screenshot viewer is a
 * fixed dialog rendered inline inside the page's scrolling content pane.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { usePinchZoom } from '../hooks/usePinchZoom'

const rect = (left: number, top: number, w: number, h: number) =>
  ({ left, top, right: left + w, bottom: top + h, width: w, height: h, x: left, y: top, toJSON: () => ({}) }) as DOMRect

function clipBox(el: HTMLElement, left: number, top: number, w: number, h: number) {
  el.style.overflowX = 'auto'
  el.style.overflowY = 'auto'
  Object.defineProperty(el, 'clientWidth', { configurable: true, value: w })
  Object.defineProperty(el, 'clientHeight', { configurable: true, value: h })
  vi.spyOn(el, 'getBoundingClientRect').mockReturnValue(rect(left, top, w, h))
}

/** A 400x300 image centred on a 1000x800 screen, at 2.5x it is 1000x750. */
function image(parent: HTMLElement) {
  const img = document.createElement('div')
  Object.defineProperty(img, 'offsetWidth', { configurable: true, value: 400 })
  Object.defineProperty(img, 'offsetHeight', { configurable: true, value: 300 })
  vi.spyOn(img, 'getBoundingClientRect').mockReturnValue(rect(300, 250, 400, 300))
  parent.appendChild(img)
  return img
}

function clampOf(img: HTMLElement) {
  const { result } = renderHook(() => usePinchZoom({ targetRef: { current: img }, enabled: false, min: 1, max: 4 }))
  return (x: number, y: number) => result.current.clampPan(x, y, 2.5)
}

beforeEach(() => {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1000 })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: 800 })
})
afterEach(() => {
  vi.restoreAllMocks()
  document.body.replaceChildren()
})

describe('usePinchZoom pan bounds', () => {
  it('bounds a target inside a fixed overlay by the screen, not a clipping page pane above it', () => {
    // The page pane sits under a 48px header and beside a 236px sidebar.
    const pane = document.createElement('div')
    clipBox(pane, 236, 48, 764, 752)
    const overlay = document.createElement('div')
    overlay.style.position = 'fixed'
    pane.appendChild(overlay)
    document.body.appendChild(pane)
    const clamp = clampOf(image(overlay))
    // Screen-bounded: the 1000px-wide image's left edge reaches x=0 at +0
    // (centre 500), its top edge y=0 at -25 (750 tall, centre 400). Bounded by
    // the pane it would stop 236px and 48px short.
    expect(clamp(10_000, 10_000)).toEqual({ x: 0, y: 0 })
    expect(clamp(-10_000, -10_000)).toEqual({ x: 0, y: 0 })
  })

  it('bounds a target by a clipping scroller inside the overlay', () => {
    const overlay = document.createElement('div')
    overlay.style.position = 'fixed'
    const scroller = document.createElement('div')
    clipBox(scroller, 44, 0, 956, 800)
    overlay.appendChild(scroller)
    document.body.appendChild(overlay)
    const clamp = clampOf(image(scroller))
    // Left edge can come in to the scroller's x=44: 500 - (500 - 44) = 44.
    expect(clamp(10_000, 0).x).toBe(44)
    // Right edge to x=1000: 1000 - 500 - 500 = 0.
    expect(clamp(-10_000, 0).x).toBe(0)
  })
})
