import { useEffect, useRef, useState, type RefObject } from 'react'

/** Accumulated same-direction travel that commits a collapse or an expand.
 *
 *  This is the "how eager" dial, and it is the one that decides whether the
 *  behavior feels twitchy — not the animation duration. At 24px a normal reading
 *  scroll flipped the chrome, so it is set well past a doubling: a flick has to
 *  be deliberate before the header moves. */
const TRIGGER_PX = 64
/** Collapsing while the first rows are still on screen would hide the toolbar
 *  before the reader has scrolled past anything, so arm only past this offset.
 *  Kept above TRIGGER_PX so the very first commit needs real distance from the
 *  top rather than just enough travel. */
const ARM_AT_PX = 96
/** At (or within a hair of) the top the chrome is always open, so a reader who
 *  scrolls back to the start never has to guess how to get the filters back. */
const TOP_EPSILON_PX = 4

/**
 * How long the collapse takes, in ms. EXPORTED because the animation and the
 * settle window below are one decision, not two: the window has to outlast the
 * transition or the reflow the transition causes lands back in the accumulator
 * and re-triggers. A caller that hardcodes its own CSS duration re-opens that
 * race silently, so the caller reads this number instead.
 */
export const COLLAPSE_MS = 509
/** Window after a state change during which scroll deltas are absorbed rather
 *  than accumulated. Derived, never a second literal — see COLLAPSE_MS. */
const SETTLE_MS = COLLAPSE_MS + 80

/**
 * Marks an element as part of the collapsing region. The hook skips scroll
 * events originating inside one, so a nested list can scroll on its own without
 * hiding the region that contains it. Exported so the caller cannot drift from
 * the selector the hook actually tests.
 */
export const CHROME_ATTR = 'data-collapse-chrome'

/**
 * Direction-driven "hide the chrome on the way down, bring it back on the way
 * up" for a scroll container nested anywhere under `hostRef`.
 *
 * The listener is registered in the CAPTURE phase because scroll events do not
 * bubble: capture still visits ancestors on the way down, so this works without
 * a handle on the scroller itself (which a virtualization library owns and does
 * not expose).
 *
 * THE SETTLE WINDOW IS LOAD-BEARING, not defensive padding. Collapsing the
 * chrome grows the scroller's `clientHeight`, which lowers its maximum
 * `scrollTop`; when the reader is near the bottom the browser clamps
 * `scrollTop` DOWNWARD to fit. That clamp is indistinguishable from a swipe up,
 * so without the window it re-expands the chrome, which shrinks the scroller,
 * which lets `scrollTop` grow again — an oscillation that reads as flicker.
 * Absorbing deltas until the transition has finished breaks that loop.
 *
 * Returns `false` at all times while `enabled` is false, so a caller can gate
 * the behavior without unmounting anything.
 */
export function useCollapseOnScroll(hostRef: RefObject<HTMLElement | null>, enabled: boolean): boolean {
  const [collapsed, setCollapsed] = useState(false)
  // Read inside the listener so a re-render does not need to re-register it.
  const collapsedRef = useRef(false)
  collapsedRef.current = collapsed

  useEffect(() => {
    if (!enabled) {
      setCollapsed(false)
      return
    }
    const host = hostRef.current
    if (!host) return

    let last = -1
    let travel = 0
    let settleUntil = 0

    const commit = (next: boolean) => {
      if (collapsedRef.current === next) return
      collapsedRef.current = next
      travel = 0
      settleUntil = performance.now() + SETTLE_MS
      setCollapsed(next)
    }

    const onScroll = (e: Event) => {
      const t = e.target as HTMLElement | null
      // `document` also dispatches scroll; only elements carry scrollTop.
      if (!t || typeof (t as HTMLElement).scrollTop !== 'number') return
      // A scroller that lives INSIDE the chrome must not drive the chrome.
      // Capture-phase delivery reaches every descendant scroller, so an expanded
      // sub-list within the collapsing region would otherwise hide the very list
      // the reader is scrolling — it would slide out from under the finger.
      if (t.closest(`[${CHROME_ATTR}]`)) return
      const y = (t as HTMLElement).scrollTop
      if (last < 0) {
        last = y
        return
      }
      const delta = y - last
      last = y
      // Checked BEFORE the settle window: reaching the top must always open the
      // chrome. Ordering it after would swallow exactly the case a reader is
      // most likely to hit — flicking hard back to the start, which lands inside
      // the window — and leave the filters hidden with the list already at row 1.
      if (y <= TOP_EPSILON_PX) {
        commit(false)
        return
      }
      if (performance.now() < settleUntil) return
      if (delta === 0) return
      // A reversal starts a fresh budget, so the reader never has to undo the
      // travel they already spent in the other direction.
      if ((delta > 0) !== (travel > 0)) travel = 0
      travel += delta
      if (travel > TRIGGER_PX && y > ARM_AT_PX) commit(true)
      else if (travel < -TRIGGER_PX) commit(false)
    }

    host.addEventListener('scroll', onScroll, { capture: true, passive: true })
    return () => host.removeEventListener('scroll', onScroll, { capture: true })
  }, [enabled, hostRef])

  return enabled && collapsed
}
