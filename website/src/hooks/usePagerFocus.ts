import { useCallback, useLayoutEffect, useRef } from 'react'

/**
 * Keeps keyboard focus on a Prev / Next pager.
 *
 * Prev is disabled on the first page and Next on the last, so pressing either
 * onto that edge disables the focused button and focus falls to the page body.
 * Once the new page renders, focus moves to the other pager button, unless the
 * user has already moved it somewhere else. A page turn made while focus was
 * not on the pager (an arrow-key shortcut from the page body) moves nothing.
 */
export function usePagerFocus() {
  const prevRef = useRef<HTMLButtonElement>(null)
  const nextRef = useRef<HTMLButtonElement>(null)
  const pending = useRef<'prev' | 'next' | null>(null)
  // No dependency list: a pager can leave the DOM while the new page loads,
  // so the check waits for a render where the pager is back. It only reads
  // refs, so running it on every render costs nothing and loops on nothing.
  // A layout effect: a passive one still queued from an earlier render can run
  // after the press and before the page turn renders, and spend the press there.
  useLayoutEffect(() => {
    if (pending.current === null) return
    const [pressed, other] = pending.current === 'prev' ? [prevRef, nextRef] : [nextRef, prevRef]
    if (!pressed.current && !other.current) return
    pending.current = null
    const active = document.activeElement
    const lost = active === null || active === document.body || active === pressed.current
    if (pressed.current?.disabled && lost) other.current?.focus()
  })
  // Stable, so a keyboard-shortcut effect can list them as dependencies.
  const record = useCallback((dir: 'prev' | 'next') => {
    const active = document.activeElement
    const onPager = active !== null && (active === prevRef.current || active === nextRef.current)
    // A paging key pressed toward an already disabled button turns no page.
    const pressed = dir === 'prev' ? prevRef.current : nextRef.current
    pending.current = onPager && !pressed?.disabled ? dir : null
  }, [])
  const pressedPrev = useCallback(() => record('prev'), [record])
  const pressedNext = useCallback(() => record('next'), [record])
  return { prevRef, nextRef, pressedPrev, pressedNext }
}
