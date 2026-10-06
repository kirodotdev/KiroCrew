import { useState, useRef, useCallback, useEffect } from 'react'
import { useTouchReplay } from '../../hooks/useTouchReplay'

/** Shared hover-label state for collapsed (icon-only) nav rows. The label is
 *  rendered through a portal anchored to the row's screen position rather than
 *  as an in-flow absolute child, because the nav's scroll container clips
 *  vertically (so a tall icon list scrolls instead of spilling out of the rail)
 *  and a vertical clip forces horizontal clipping too, which would chop the
 *  flyout at the 58px rail edge. Repositions while shown so it follows the row
 *  when the rail is scrolled/resized. */
export function useNavTip<T extends HTMLElement>(enabled: boolean) {
  const [tip, setTip] = useState<{ top: number; left: number; height: number } | null>(null)
  const [tipOn, setTipOn] = useState(false) // drives the opacity fade
  const rowRef = useRef<T | null>(null)
  const hideTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const rafId = useRef<number | null>(null)
  const place = useCallback(() => {
    if (!rowRef.current) return
    const r = rowRef.current.getBoundingClientRect()
    // Overlay the row exactly (same top-left + height) so the flyout reads as
    // the collapsed row expanding in place. Bail out (return the same object) if
    // nothing moved — the scroll listener fires on any document scroll, so this
    // avoids needless re-renders when the rail itself didn't move.
    setTip(prev =>
      prev && prev.top === r.top && prev.left === r.left && prev.height === r.height
        ? prev
        : { top: r.top, left: r.left, height: r.height }
    )
  }, [])
  // A touch tap's replayed mouseenter (or the focus its mousedown gives the
  // row) must not mount the label: on iOS that reads the tap as a hover and
  // drops the row's click. Consumers spread `pointerProps` on the row so the
  // tap's own pointer type is recorded (see useTouchReplay).
  const { fromTouch, pointerProps } = useTouchReplay()
  const showTip = useCallback(() => {
    if (!enabled || !rowRef.current || fromTouch()) return
    if (hideTimer.current) { clearTimeout(hideTimer.current); hideTimer.current = null }
    place()
    // Mount at opacity 0, then flip next frame so the CSS opacity transition
    // runs (a portal can't fade if it mounts already-visible). Track the handle
    // so a fast hover-out can cancel it — otherwise the rAF fires after hideTip
    // and flashes the label to full opacity before the unmount timer.
    if (rafId.current != null) cancelAnimationFrame(rafId.current)
    rafId.current = requestAnimationFrame(() => { rafId.current = null; setTipOn(true) })
  }, [enabled, place, fromTouch])
  const hideTip = useCallback(() => {
    if (rafId.current != null) { cancelAnimationFrame(rafId.current); rafId.current = null }
    setTipOn(false)
    hideTimer.current = setTimeout(() => setTip(null), 150) // keep mounted for fade-out
  }, [])
  // Dismiss with NO fade-out, for rows whose label text changes on activation
  // (the Apps overflow toggle flips "N more" <-> "Show less"). A fading label
  // stays mounted through the re-render, so it would flash the OPPOSITE label
  // as a ghost at the old coordinates before unmounting.
  const dismissTip = useCallback(() => {
    if (hideTimer.current) { clearTimeout(hideTimer.current); hideTimer.current = null }
    if (rafId.current != null) { cancelAnimationFrame(rafId.current); rafId.current = null }
    setTipOn(false)
    setTip(null)
  }, [])
  // While shown, follow the row on scroll/resize (capture:true catches the
  // nav's inner scroll container, which doesn't bubble scroll to window).
  // Depend on a stable boolean — not `tip` itself — so the listeners subscribe
  // once when the label appears and unsubscribe once when it goes, instead of
  // churning on every position update `place()` makes during a scroll.
  const tipVisible = tip !== null
  useEffect(() => {
    if (!tipVisible) return
    window.addEventListener('scroll', place, true)
    window.addEventListener('resize', place)
    return () => {
      window.removeEventListener('scroll', place, true)
      window.removeEventListener('resize', place)
    }
  }, [tipVisible, place])
  // Reset when the row stops being collapsible (sidebar expands while a tip is
  // up). mouseLeave may never fire if the cursor stays over the row as it grows,
  // which would otherwise leave the scroll/resize listeners attached and firing
  // place() on every document scroll even though the portal no longer renders.
  useEffect(() => {
    if (enabled) return
    if (hideTimer.current) { clearTimeout(hideTimer.current); hideTimer.current = null }
    if (rafId.current != null) { cancelAnimationFrame(rafId.current); rafId.current = null }
    setTip(null)
    setTipOn(false)
  }, [enabled])
  useEffect(() => () => {
    if (hideTimer.current) clearTimeout(hideTimer.current)
    if (rafId.current != null) cancelAnimationFrame(rafId.current)
  }, [])
  return { tip, tipOn, rowRef, showTip, hideTip, dismissTip, pointerProps }
}
