import { useEffect, useRef, useState, type RefObject } from 'react'

/** The visible band must be at least this much shorter than the layout viewport
 *  before it counts as keyboard-reduced — matches `useKeyboardViewportReset`'s
 *  own threshold. Large enough to ignore Safari's collapsing URL bar
 *  (~50-80px), small enough to catch a docked keyboard and the iPad shortcut
 *  bar. Below it, or while the page is pinch-zoomed, there is no lift. */
const KEYBOARD_THRESHOLD_PX = 120

/**
 * How far the composer's bottom edge is hidden below the visible band, in CSS
 * px — the amount to lift it by so it clears an on-screen keyboard.
 *
 * Why a MEASUREMENT and not a fixed inset: the composer is a normal in-flow
 * element at the bottom of the height-locked (`h-dvh`), `overflow:hidden`
 * dashboard shell, not a `fixed` overlay like the command palette or the
 * sessions/members drawers (which lift themselves by the full
 * `innerHeight - visualViewport.(offsetTop+height)` gap). A software keyboard
 * shrinks only the VISUAL viewport on iOS/iPadOS; the layout viewport the shell
 * fills keeps its height, so the composer row stays at the layout bottom,
 * behind the keyboard.
 *
 * iOS Safari ALSO pans the layout viewport to bring the focused input into the
 * visible band, and on iPhone that pan is what lifts the composer (preserved by
 * the keyboard-open detection in `useKeyboardViewportReset`). On iPadOS the pan
 * does not place the composer fully above the keyboard. Lifting by a FIXED
 * keyboard inset would double up with the pan on iPhone (composer pushed up
 * twice); measuring the composer's own remaining overlap self-corrects instead:
 *
 *   lift = max(0, composerBottom - (visualViewport.offsetTop + height))
 *
 * When a pan already placed the composer inside the visible band the overlap is
 * 0 and this is a no-op; when the keyboard still covers it (iPad) the overlap is
 * exactly how far to lift. It is also 0 on desktop and whenever no keyboard is
 * up (the visible band reaches the layout bottom), so every consumer is a strict
 * no-op at rest.
 *
 * The lift is gated to a real on-screen keyboard: a desktop trackpad pinch-zoom
 * also shrinks the visual viewport (and stays enabled on desktop), so the hook
 * returns 0 whenever the page is zoomed (`visualViewport.scale !== 1`) or the
 * viewport HEIGHT is reduced by less than the keyboard threshold
 * (`innerHeight - visualViewport.height`, which excludes the pan offset so an
 * open keyboard is not read as closed while Safari pans). A collapsing URL bar
 * stays below the threshold and does not lift.
 *
 * Returns 0 where `visualViewport` is unavailable, which makes the hook a no-op
 * rather than a hazard.
 */
export function useComposerKeyboardLift(ref: RefObject<HTMLElement>): number {
  const [lift, setLift] = useState(0)
  // The lift currently applied to the measured element as a `translateY`. The
  // measurement reads the element's OWN rect, which already carries that
  // transform, so the applied amount is added back to recover the untransformed
  // bottom — otherwise each re-measure would read the lifted rect, compute an
  // overlap of ~0 and collapse the lift, flickering the composer back behind
  // the keyboard. Kept in a ref so the rAF callback sees the latest value
  // without re-subscribing its listeners.
  const liftRef = useRef(0)

  useEffect(() => {
    if (typeof window === 'undefined') return
    const vv = window.visualViewport
    if (!vv) return

    let frame: number | null = null
    const measure = () => {
      frame = null
      const el = ref.current
      if (!el) {
        liftRef.current = 0
        setLift((prev) => (prev === 0 ? prev : 0))
        return
      }
      // Lift ONLY for an on-screen keyboard, never for a desktop pinch-zoom,
      // which also shrinks the visual viewport and moves its origin. Pinch-zoom
      // stays enabled on desktop (see utils/pageZoom.ts), where the viewport
      // meta does not apply, so a zoom fires the same resize/scroll events. Two
      // guards: `vv.scale !== 1` is the engine's own "page is zoomed" answer,
      // and a VIEWPORT-HEIGHT reduction below the keyboard threshold (a
      // collapsing URL bar, a sub-keyboard shrink) is not a keyboard either.
      //
      // The height reduction is `innerHeight - vv.height` WITHOUT `offsetTop`:
      // iOS/iPadOS pans the page to reveal the focused input, which raises
      // `offsetTop` while the keyboard is fully open. Folding `offsetTop` into
      // this test would shrink the measured reduction as Safari pans and could
      // drop it below the threshold mid-keyboard, classifying an open keyboard
      // as closed and resetting the lift to 0 with the composer still covered.
      // `offsetTop` belongs only in the remaining-overlap calculation below.
      const zoomed = vv.scale !== 1
      const viewportHeightReducedBy = window.innerHeight - vv.height
      const keyboardOpen = !zoomed && viewportHeightReducedBy > KEYBOARD_THRESHOLD_PX
      if (!keyboardOpen) {
        liftRef.current = 0
        setLift((prev) => (prev === 0 ? prev : 0))
        return
      }
      // The element already carries `translateY(-liftRef.current)`, so its
      // measured bottom is that much higher than its untransformed position.
      // Add the applied lift back to measure as if no transform were present —
      // this makes the computation idempotent: once lifted, a re-measure on a
      // visual-viewport / scroll event reads the SAME overlap instead of seeing
      // the lifted rect clear the keyboard and collapsing the lift to 0.
      const untransformedBottom = el.getBoundingClientRect().bottom + liftRef.current
      // getBoundingClientRect is relative to the LAYOUT viewport; the visible
      // band's bottom edge in those coordinates is offsetTop + height.
      const visibleBottom = vv.offsetTop + vv.height
      const next = Math.max(0, Math.round(untransformedBottom - visibleBottom))
      liftRef.current = next
      setLift((prev) => (prev === next ? prev : next))
    }
    const schedule = () => {
      if (frame !== null) return
      frame = window.requestAnimationFrame(measure)
    }

    schedule()
    vv.addEventListener('resize', schedule)
    vv.addEventListener('scroll', schedule)
    window.addEventListener('resize', schedule)
    // A capture-phase scroll covers inner-container scrolls that do not bubble
    // to window (the transcript), which move the composer's measured bottom.
    window.addEventListener('scroll', schedule, true)
    return () => {
      if (frame !== null) window.cancelAnimationFrame(frame)
      vv.removeEventListener('resize', schedule)
      vv.removeEventListener('scroll', schedule)
      window.removeEventListener('resize', schedule)
      window.removeEventListener('scroll', schedule, true)
    }
  }, [ref])

  return lift
}
