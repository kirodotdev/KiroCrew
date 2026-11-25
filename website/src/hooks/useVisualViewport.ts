import { useEffect, useState } from 'react'

/**
 * The VISUAL viewport -- the part of the window the user can actually see.
 *
 * A software keyboard shrinks the visual viewport. Whether it also shrinks the
 * LAYOUT viewport is a per-browser decision: Chromium honours the
 * `interactive-widget=resizes-content` hint in the viewport meta and resizes both,
 * but iOS Safari ignores that key entirely and resizes only the visual one. There,
 * `position: fixed; inset: 0` and every `vh` / `dvh` length keep measuring a window
 * whose bottom is behind the keyboard, so a focused full-screen overlay strands its
 * own lower half with no gesture that reaches it.
 *
 * iOS also scrolls the page to reveal the focused input, which moves the visual
 * viewport's origin -- hence `offsetTop` alongside the height. An overlay pinned to
 * both follows the keyboard on every browser without asking which one it is on.
 *
 * Falls back to the layout viewport where `visualViewport` is unavailable, which
 * makes the hook a no-op rather than a hazard.
 */
export interface VisualViewport {
  /** Visible height in CSS px. */
  height: number
  /** Distance from the layout viewport's top to the visual viewport's top. */
  offsetTop: number
}

/**
 * Whether the user has pinch-zoomed the page in. A sideways swipe then pans the
 * zoomed view, so the side panel swipes leave it to the browser. The small
 * margin keeps a scale that rounds a hair over 1 from counting as zoomed.
 */
export function pinchZoomed(): boolean {
  const scale = typeof window !== 'undefined' ? window.visualViewport?.scale : undefined
  return typeof scale === 'number' && scale > 1.01
}

const read = (): VisualViewport => {
  const vv = typeof window !== 'undefined' ? window.visualViewport : null
  if (!vv) {
    return { height: typeof window !== 'undefined' ? window.innerHeight : 0, offsetTop: 0 }
  }
  return { height: vv.height, offsetTop: vv.offsetTop }
}

export function useVisualViewport(): VisualViewport {
  const [box, setBox] = useState<VisualViewport>(read)

  useEffect(() => {
    const vv = window.visualViewport
    // `scroll` matters as much as `resize`: iOS keeps the height and moves the
    // origin when it scrolls the focused input into view.
    // An unchanged box keeps its object, so a read re-renders nothing.
    const onChange = () => {
      const next = read()
      setBox(prev => (prev.height === next.height && prev.offsetTop === next.offsetTop ? prev : next))
    }
    if (vv) {
      vv.addEventListener('resize', onChange)
      vv.addEventListener('scroll', onChange)
    }
    window.addEventListener('resize', onChange)
    // The catch-up read for a change between render and this effect waits for
    // the next frame: reading the viewport here forces a layout of the page
    // inside the commit's task.
    const first = requestAnimationFrame(onChange)
    return () => {
      cancelAnimationFrame(first)
      if (vv) {
        vv.removeEventListener('resize', onChange)
        vv.removeEventListener('scroll', onChange)
      }
      window.removeEventListener('resize', onChange)
    }
  }, [])

  return box
}
