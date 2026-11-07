import { useEffect, useState, type RefObject } from 'react'

/**
 * How far a bottom-docked box must rise so its bottom edge sits above the
 * on-screen keyboard, while it holds keyboard focus. 0 otherwise.
 *
 * iOS Safari shrinks only the visual viewport for the keyboard, so a box at the
 * bottom of the height-locked shell stays behind it unless Safari happens to pan
 * the page far enough when the field gains focus. The lift is how far the box's
 * own bottom edge sits below the visible band (`offsetTop + height`), so a box
 * that already sits above the layout bottom (the chat composer over a docked
 * terminal) rises only by the part the keyboard covers. Whatever part of the
 * keyboard height Safari's own pan already covers is subtracted through
 * `offsetTop`, so the two never add up to more than the keyboard.
 *
 * The caller spends the lift as bottom padding INSIDE the measured box, so the
 * box's bottom edge stays put and its content rises: re-measuring a lifted box
 * gives the same lift, and the extra height is real layout space that the
 * surrounding layout makes room for.
 *
 * One box's lift moves the boxes laid out above it (a docked terminal dropping
 * its lift when focus moves to the composer moves the composer down), and no
 * viewport or focus event follows that layout change, so every mounted lift
 * re-measures after any lift changes.
 */
const remeasureAll = new Set<() => void>()

export function useKeyboardLift(ref: RefObject<HTMLElement | null>, enabled: boolean): number {
  const [lift, setLift] = useState(0)

  useEffect(() => { remeasureAll.forEach(update => update()) }, [lift])

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
      const bottom = el ? el.getBoundingClientRect().bottom : 0
      const gap = zoomed ? 0 : bottom - vv.offsetTop - vv.height
      setLift(focused ? Math.max(0, Math.round(gap)) : 0)
    }
    const update = () => { if (!frame) frame = requestAnimationFrame(measure) }
    vv.addEventListener('resize', update)
    vv.addEventListener('scroll', update)
    document.addEventListener('focusin', update)
    document.addEventListener('focusout', update)
    remeasureAll.add(update)
    measure()
    return () => {
      if (frame) cancelAnimationFrame(frame)
      remeasureAll.delete(update)
      vv.removeEventListener('resize', update)
      vv.removeEventListener('scroll', update)
      document.removeEventListener('focusin', update)
      document.removeEventListener('focusout', update)
    }
  }, [ref, enabled])

  return lift
}
