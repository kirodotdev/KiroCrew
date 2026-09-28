import { useSyncExternalStore } from 'react'

/* Exported for the top-bar rung-budget test: the <640px icon-only rung base in
   index.css needs the desktop readouts to be unreachable below the pill's
   label gate, which holds only while this form switch sits at or above it. */
export const MOBILE_BREAKPOINT = 768
const MOBILE_QUERY = `(max-width: ${MOBILE_BREAKPOINT - 1}px)`

// Lazy, cached on matchMedia's function identity: an import evaluated before matchMedia
// exists must not latch null, and a consumer sharing this store keeps render-time swaps.
let cachedFn: unknown
let cachedMql: MediaQueryList | null = null

function currentMql(): MediaQueryList | null {
  const fn = typeof window !== 'undefined' ? window.matchMedia : undefined
  if (typeof fn !== 'function') return null
  if (fn !== cachedFn) {
    cachedFn = fn
    cachedMql = fn.call(window, MOBILE_QUERY)
  }
  return cachedMql
}

// A single missed or deferred `change` delivery used to strand the breakpoint;
// these only notify — getSnapshot still reads .matches, so a no-op wake bails.
const WINDOW_RECHECK_EVENTS = ['orientationchange', 'resize', 'pageshow'] as const
// visibilitychange fires at the document, not the window.
const DOCUMENT_RECHECK_EVENTS = ['visibilitychange'] as const

function subscribe(cb: () => void) {
  // Closed over, so the cleanup detaches from the SAME list this attached to even if
  // `window.matchMedia` is replaced while the subscription is live.
  const mql = currentMql()
  mql?.addEventListener('change', cb)
  if (typeof window !== 'undefined') {
    for (const type of WINDOW_RECHECK_EVENTS) window.addEventListener(type, cb)
    for (const type of DOCUMENT_RECHECK_EVENTS) document.addEventListener(type, cb)
  }
  return () => {
    mql?.removeEventListener('change', cb)
    if (typeof window !== 'undefined') {
      for (const type of WINDOW_RECHECK_EVENTS) window.removeEventListener(type, cb)
      for (const type of DOCUMENT_RECHECK_EVENTS) document.removeEventListener(type, cb)
    }
  }
}

function getSnapshot() {
  return currentMql()?.matches ?? false
}

function getServerSnapshot() {
  return false
}

/**
 * The VIEWPORT half of `useIsMobile`, carve-out and all recovery, minus the `/embed/`
 * override. Exported so a consumer that must track the breakpoint VERBATIM shares this
 * module's single subscription instead of opening a second one.
 *
 * NOTE for a future export here: ~31 suites partial-mock this module as
 * `{ useIsMobile: ... }`, so anything else imported from it is `undefined` in those tests
 * -- silent for a constant, a throw for a function. Check the renderers first.
 */
export function useIsNarrowViewport() {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot)
}

export function useIsMobile() {
  const match = useIsNarrowViewport()
  // In embed mode (IntelliJ plugin minimal view), never report as mobile
  // regardless of viewport width. The plugin panel can be narrow but should
  // always behave as desktop (Enter to send, full icon row, no collapsed UI).
  if (typeof window !== 'undefined' && window.location.pathname.startsWith('/embed/')) return false
  return match
}
