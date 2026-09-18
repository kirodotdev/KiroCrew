import { useEffect } from 'react'

/**
 * Keep a portaled popover glued to its trigger while it is open.
 *
 * The composer popovers (busy-send picker, "+" menu, mic-source menu) portal to
 * <body> and position themselves from a snapshot of the trigger's
 * `getBoundingClientRect()`. Anything that moves the trigger after that snapshot
 * detaches the menu from it: the composer growing, a window resize, a scroll in
 * an inner container, and -- the case `window` events alone miss -- the mobile
 * (iOS) software keyboard opening or closing, which announces itself only on
 * `window.visualViewport`. See `useVisualViewport` for why the visual viewport
 * is its own event source.
 *
 * While `open`, this effect re-runs `measure` on every window resize,
 * capture-phase scroll (inner-container scrolls do not bubble to `window`), and
 * visual-viewport resize/scroll, coalesced to one animation frame so an event
 * burst (a keyboard-driven resize fires resize AND scroll) measures once per
 * frame. It also measures once on open, so a host whose trigger moved between
 * the toggle's own measurement and the first paint self-corrects.
 *
 * `measure` is the host's own reading of its trigger into its own positioning
 * state -- pass a stable `useCallback`, or the listeners churn every render.
 */
export function useAnchorRemeasure(open: boolean, measure: () => void): void {
  useEffect(() => {
    if (!open) return
    let frame: number | null = null
    const scheduleMeasure = () => {
      if (frame !== null) return
      frame = window.requestAnimationFrame(() => {
        frame = null
        measure()
      })
    }
    const viewport = window.visualViewport

    scheduleMeasure()
    window.addEventListener('resize', scheduleMeasure)
    window.addEventListener('scroll', scheduleMeasure, true)
    viewport?.addEventListener('resize', scheduleMeasure)
    viewport?.addEventListener('scroll', scheduleMeasure)
    return () => {
      if (frame !== null) window.cancelAnimationFrame(frame)
      window.removeEventListener('resize', scheduleMeasure)
      window.removeEventListener('scroll', scheduleMeasure, true)
      viewport?.removeEventListener('resize', scheduleMeasure)
      viewport?.removeEventListener('scroll', scheduleMeasure)
    }
  }, [open, measure])
}
