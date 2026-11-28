import { useLayoutEffect, useState, type RefObject } from 'react'

/** The touch target a bar control gets under a coarse pointer (`mc-touch-hit`). */
export const BAR_TOUCH_TARGET_PX = 44
/** Space kept between the name chip and the nearest control's touch target. */
export const BAR_STACK_CLEARANCE_PX = 4

/** A box's horizontal extent, as `getBoundingClientRect()` reports it. */
export interface Span {
  left: number
  right: number
}

/**
 * The widest the Bot's identity stack may be in the phone bar, in px.
 *
 * The stack is centred on the viewport (`centre`), so it may reach out from the
 * centre only as far as the nearer side allows. A side reaches in to the inner
 * edge of its innermost control's touch target, which is at least
 * `BAR_TOUCH_TARGET_PX` wide even when the control draws smaller; with no
 * control, to the header's own edge (`bounds`). The two sides are not equal:
 * a remote crew puts the crew switcher before the drawer toggle, and the trail
 * holds the search and panel openers.
 */
export function barStackMaxWidth(centre: number, bounds: Span, lead: Span[], trail: Span[]): number {
  const half = (s: Span) => Math.max(s.right - s.left, BAR_TOUCH_TARGET_PX) / 2
  const mid = (s: Span) => (s.left + s.right) / 2
  const leadReach = Math.max(bounds.left, ...lead.map(s => mid(s) + half(s)))
  const trailReach = Math.min(bounds.right, ...trail.map(s => mid(s) - half(s)))
  const room = Math.min(centre - leadReach, trailReach - centre) - BAR_STACK_CLEARANCE_PX
  return Math.max(0, Math.floor(2 * room))
}

/** The cell's visible controls: a hidden one (`display: none`) measures 0 wide. */
function controls(cell: HTMLElement | null): HTMLElement[] {
  if (!cell) return []
  return [...cell.querySelectorAll<HTMLElement>('button, a[href]')].filter(el => el.getBoundingClientRect().width > 0)
}

/**
 * `barStackMaxWidth` for the live bar, re-measured when the bar or a control in
 * it changes size, when a control mounts or leaves, and on a window resize.
 * `null` while `enabled` is false or nothing has a layout (jsdom): the caller
 * keeps its class-based fallback then.
 */
export function useBarStackCap(
  enabled: boolean,
  header: RefObject<HTMLElement | null>,
  lead: RefObject<HTMLElement | null>,
  trail: RefObject<HTMLElement | null>,
): number | null {
  const [cap, setCap] = useState<number | null>(null)
  useLayoutEffect(() => {
    const bar = header.current
    if (!enabled || !bar) {
      setCap(null)
      return
    }
    const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => measure())
    function measure() {
      const width = document.documentElement.clientWidth
      const box = bar!.getBoundingClientRect()
      if (!width || !box.width) {
        setCap(null)
        return
      }
      const leadControls = controls(lead.current)
      const trailControls = controls(trail.current)
      // `observe` is a no-op for an element already observed.
      for (const el of [...leadControls, ...trailControls]) resize?.observe(el)
      const rects = (els: HTMLElement[]) => els.map(el => el.getBoundingClientRect())
      setCap(barStackMaxWidth(width / 2, box, rects(leadControls), rects(trailControls)))
    }
    measure()
    resize?.observe(bar)
    const mutation = new MutationObserver(measure)
    for (const cell of [lead.current, trail.current]) {
      if (cell) mutation.observe(cell, { childList: true, subtree: true })
    }
    window.addEventListener('resize', measure)
    return () => {
      resize?.disconnect()
      mutation.disconnect()
      window.removeEventListener('resize', measure)
    }
  }, [enabled, header, lead, trail])
  return cap
}
