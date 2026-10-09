import { useCallback, useRef, useState, type RefObject } from 'react'

/**
 * The composer dock that floats over the bottom of a transcript scroller (the
 * iOS toolbar layout): the scroller runs the full height of its pane, the
 * conversation scrolls UNDER the glass, and the scroller pays for the covered
 * strip with `paddingBottom: dockH + DOCK_CLEARANCE_PX`, measured from the dock
 * by the hook below. Shared by the main chat (`ChatPage.tsx`) and the pane
 * (`components/ChatPane.tsx`: split panes and a crewmate's chat), so both hosts
 * place the last line against the glass by the same measurement
 * (`docs/decisions/2026-10-02-chat-transcript-scrolls-under-the-composer-glass.md`).
 */

/**
 * Breathing room, in px, between the composer dock's top edge and the last line of
 * the transcript, on top of the dock's own measured height.
 *
 * The transcript scroller runs the full height of the pane and the dock floats
 * over its bottom edge (iOS toolbar layout), so the scroller's `paddingBottom` is
 * `dockH + this`: exactly the strip the dock covers, plus this margin, so the last
 * line stops clear of the glass instead of under it. There is no opaque fade band
 * between the two — the transcript scrolls under the glass and the material's own
 * blur and tint are what keep the dock legible over it. A px value and not `vh`
 * on purpose: as `2vh` the clearance tracked the viewport and the margin was one
 * pixel on a phone. ChatPage.dockClearance.test.tsx pins the wiring.
 */
export const DOCK_CLEARANCE_PX = 16

/**
 * The dock's measured height and scrollbar gutter, plus the composer box ref
 * (the quote flight's target).
 */
export function useComposerDockMetrics(scrollerRef: RefObject<HTMLDivElement | null>) {
  const inputAreaRef = useRef<HTMLDivElement>(null)
  // The composer dock floats over the bottom of the transcript scroller, so the
  // scroller has to be told how much of its bottom edge is covered. Measured
  // rather than summed from parts: the dock's height is whatever the status
  // stack, the follow-up chips, the approval bar and the composer's own growth
  // add up to at this instant, and every one of those changes independently.
  // A callback ref, not a mount effect: the dock lives inside the pane's
  // conditional branch, so a `[]` effect can run before it exists and never
  // look again. The ref fires in the commit phase each time the box mounts or
  // unmounts, and its synchronous setState lands before paint — the first
  // painted frame already carries the right padding, where an effect-timed
  // measurement paints one frame with the last line under the glass, then jumps.
  const [dockH, setDockH] = useState(0)
  // The scroller reserves a `scrollbar-gutter: stable` column on its right, and
  // its rows are centred in the content box that EXCLUDES that column. The dock
  // is inset by the same width, so its column lines up with the transcript's and
  // the thumb stays uncovered down to the pane's bottom edge. Measured, not the
  // 6px the stylesheet asks for: an engine that ignores `::-webkit-scrollbar`
  // reserves its own width.
  const [dockGutter, setDockGutter] = useState(0)
  const dockObserverRef = useRef<ResizeObserver | null>(null)
  const dockRef = useCallback((el: HTMLDivElement | null) => {
    dockObserverRef.current?.disconnect()
    dockObserverRef.current = null
    if (!el) { setDockH(0); setDockGutter(0); return }
    const measure = () => {
      setDockH(el.offsetHeight)
      const sc = scrollerRef.current
      setDockGutter(sc ? Math.max(0, sc.offsetWidth - sc.clientWidth) : 0)
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    // The gutter is the scroller's own reserved column, so watch the scroller
    // too: an engine with overlay scrollbars changes that width without the
    // dock resizing.
    if (scrollerRef.current) ro.observe(scrollerRef.current)
    dockObserverRef.current = ro
  }, [scrollerRef])
  return { inputAreaRef, dockH, dockGutter, dockRef }
}
