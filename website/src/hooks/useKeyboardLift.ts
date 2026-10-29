import { useEffect, useState, type RefObject } from 'react'

/**
 * How far a bottom-docked box must rise so its bottom edge sits above the
 * on-screen keyboard, while it holds keyboard focus. 0 otherwise.
 *
 * iOS Safari shrinks only the visual viewport for the keyboard, so a box at the
 * bottom of the height-locked shell stays behind it unless Safari happens to pan
 * the page far enough when the field gains focus. The gap is measured against
 * `documentElement.clientHeight` (the layout height the shell is laid out in):
 * iPhone Safari shrinks `innerHeight` along with the visual viewport, so a gap
 * taken from `innerHeight` reads 0 there. Whatever part of the keyboard height
 * Safari's own pan already covers is subtracted through `offsetTop`, so the two
 * never add up to more than the keyboard.
 */
export function useKeyboardLift(ref: RefObject<HTMLElement | null>, enabled: boolean): number {
  const [lift, setLift] = useState(0)

  useEffect(() => {
    if (!enabled || typeof window === 'undefined') {
      setLift(0)
      return
    }
    const vv = window.visualViewport
    if (!vv) return
    // The keyboard animates in with a resize and a scroll event per frame;
    // read the viewport once per frame instead of once per event.
    let frame = 0
    const measure = () => {
      frame = 0
      const el = ref.current
      const focused = !!el && el.contains(document.activeElement)
      // Pinch-zoom shrinks the visual viewport too; only an unzoomed viewport
      // can mean a keyboard.
      const zoomed = Math.abs((vv.scale ?? 1) - 1) > 0.01
      const gap = zoomed ? 0 : document.documentElement.clientHeight - vv.offsetTop - vv.height
      setLift(focused ? Math.max(0, Math.round(gap)) : 0)
    }
    const update = () => { if (!frame) frame = requestAnimationFrame(measure) }
    vv.addEventListener('resize', update)
    vv.addEventListener('scroll', update)
    document.addEventListener('focusin', update)
    document.addEventListener('focusout', update)
    measure()
    return () => {
      if (frame) cancelAnimationFrame(frame)
      vv.removeEventListener('resize', update)
      vv.removeEventListener('scroll', update)
      document.removeEventListener('focusin', update)
      document.removeEventListener('focusout', update)
    }
  }, [ref, enabled])

  return lift
}
