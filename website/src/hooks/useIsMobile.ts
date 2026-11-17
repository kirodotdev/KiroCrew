import { useSyncExternalStore } from 'react'
import { appPathname } from '@/lib/basePath'
import { runLayoutFormChange } from '@/lib/layoutFormTransition'
import { touchFirstForm } from '@/lib/touchLayoutForm'

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
// these only notify — a no-op wake bails.
const WINDOW_RECHECK_EVENTS = ['orientationchange', 'resize', 'pageshow'] as const
// visibilitychange fires at the document, not the window.
const DOCUMENT_RECHECK_EVENTS = ['visibilitychange'] as const

// ONE set of listeners for every consumer, so a form change reaches all of them
// in the same synchronous update: `runLayoutFormChange` wraps that update in a
// view transition, which needs the whole old form captured before ANY consumer
// re-renders and the whole new form in place when its callback returns. A
// listener per consumer would let consumers flip one at a time.
const listeners = new Set<() => void>()
// The form every consumer is currently rendering. Moves only when the listeners
// are notified, so a re-render that happens between the media query flipping
// and the transition's callback (another resize handler's state update) still
// draws the old form. `null` while nothing is subscribed: reads go live.
let committed: boolean | null = null
let attachedMql: MediaQueryList | null = null

// A touch-first device takes the form its browser asks for (touchLayoutForm);
// every other device keeps the width breakpoint. The resize and orientation
// listeners below re-read both, so a rotation or a desktop-site reload lands.
function readLive() {
  if (viewportless()) return committed ?? touchFirstForm() ?? false
  return touchFirstForm() ?? currentMql()?.matches ?? false
}

// A document with no viewport at all reports 0x0 and matches the narrow query:
// an iframe whose element is display:none, which is how InstancesViewport keeps
// every inactive remote pane warm. Nobody sees that width, so it never decides
// the form: the pane keeps the form it last drew (desktop before any real
// measurement) instead of redrawing as a phone while hidden.
function viewportless(): boolean {
  return typeof window !== 'undefined' && window.innerWidth === 0 && window.innerHeight === 0
}

// Whether the last signal found no viewport. A form change noticed as the frame
// is shown again is not a resize the reader watched, so it swaps instantly.
let wasViewportless = false

/** The form on <html> as `data-layout-form`, for CSS: `md:` and up only
 *  apply off the phone form (tailwind-theme.css), so a phone drawn past 768px
 *  keeps its phone-only controls. Never `phone` in an embed frame, which is
 *  always drawn desktop. */
function markForm(phone: boolean) {
  if (typeof document === 'undefined') return
  const form = phone && !appPathname().startsWith('/embed/') ? 'phone' : 'desktop'
  if (document.documentElement.dataset.layoutForm !== form) document.documentElement.dataset.layoutForm = form
}
markForm(readLive())

function notifyAll() {
  if (committed !== null) markForm(committed)
  for (const cb of [...listeners]) cb()
}

// The form a started switch is heading to, until its update runs. One resize
// fires both the list's `change` and the window's `resize`, and a view
// transition runs its update a frame later, so the second signal arrives while
// the first switch is still pending; starting another would cut the first one
// short.
let pending: boolean | null = null

function onSignal(e?: Event) {
  const revealed = wasViewportless
  wasViewportless = viewportless()
  const next = readLive()
  if (next === pending) return
  if (committed === null || next === committed || revealed) { pending = null; committed = next; notifyAll(); return }
  pending = next
  // The update commits the form the viewport has WHEN it runs: a resize back
  // across the breakpoint before then leaves nothing to switch.
  // Every consumer may have unmounted before it runs; reads then stay live.
  runLayoutFormChange(() => {
    pending = null
    if (listeners.size === 0) return
    committed = readLive()
    notifyAll()
  }, { toNarrow: next, cause: e?.type ?? 'change' })
}

function attach() {
  // Closed over, so detaching reaches the SAME list this attached to even if
  // `window.matchMedia` is replaced while the subscription is live.
  attachedMql = currentMql()
  wasViewportless = viewportless()
  attachedMql?.addEventListener('change', onSignal)
  if (typeof window !== 'undefined') {
    for (const type of WINDOW_RECHECK_EVENTS) window.addEventListener(type, onSignal)
    for (const type of DOCUMENT_RECHECK_EVENTS) document.addEventListener(type, onSignal)
  }
}

function detach() {
  attachedMql?.removeEventListener('change', onSignal)
  attachedMql = null
  if (typeof window !== 'undefined') {
    for (const type of WINDOW_RECHECK_EVENTS) window.removeEventListener(type, onSignal)
    for (const type of DOCUMENT_RECHECK_EVENTS) document.removeEventListener(type, onSignal)
  }
}

function subscribe(cb: () => void) {
  if (listeners.size === 0) {
    attach()
    committed = readLive()
    markForm(committed)
  } else if (currentMql() !== attachedMql) {
    // `window.matchMedia` was replaced under a live subscription: follow the new list.
    attachedMql?.removeEventListener('change', onSignal)
    attachedMql = currentMql()
    attachedMql?.addEventListener('change', onSignal)
  }
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
    if (listeners.size === 0) { detach(); committed = null; pending = null }
  }
}

function getSnapshot() {
  return committed ?? readLive()
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
  if (typeof window !== 'undefined' && appPathname().startsWith('/embed/')) return false
  return match
}
