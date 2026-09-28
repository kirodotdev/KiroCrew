import { useLayoutEffect, useState } from 'react'

/**
 * Resolve a portal target the App shell renders by element id, for a page that
 * fills it with `createPortal`.
 *
 * The shell sits outside the router, so on a route change the slot is usually
 * already in the DOM and the lazy initialiser finds it on render 1 — an
 * effect-only lookup would leave the first paint on the inline fallback and
 * then flash it into the slot. On a COLD load the shell and the page render in
 * one pass, so the initialiser runs before the slot is committed: the lookup
 * therefore repeats in a LAYOUT effect, which runs after the commit and before
 * paint, so the first painted frame already has the row in the bar rather than
 * one frame of the inline fallback. The MutationObserver covers the remaining
 * case: a breakpoint crossing can flush this component's media-query
 * subscription BEFORE the shell re-renders the slot, so a one-shot lookup would
 * miss it forever; the observer waits for the element, latches it, and stops.
 *
 * Returns `null` while `enabled` is false or the slot does not exist, which is
 * the caller's signal to render the fallback in place.
 */
export function useShellSlot(id: string, enabled: boolean): HTMLElement | null {
  const [slot, setSlot] = useState<HTMLElement | null>(
    () => (enabled ? document.getElementById(id) : null),
  )
  useLayoutEffect(() => {
    if (!enabled) { setSlot(null); return }
    const now = document.getElementById(id)
    if (now) { setSlot(now); return }
    setSlot(null)
    const mo = new MutationObserver(() => {
      const found = document.getElementById(id)
      if (found) { setSlot(found); mo.disconnect() }
    })
    mo.observe(document.body, { childList: true, subtree: true })
    return () => mo.disconnect()
  }, [id, enabled])
  return slot
}
