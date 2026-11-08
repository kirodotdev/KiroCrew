/**
 * The trackpad's real finger-down / finger-up, when the page can know it.
 *
 * A page sees a two-finger swipe only as `wheel` events, so a plain browser has
 * to GUESS the lift-off (hooks/useTrackpadSwipe: a gap, or the momentum tail's
 * shape). The desktop app's shell knows it exactly from Chromium's gesture
 * scroll events (electron/scroll-touch.js, macOS only) and reports each change
 * as `begin` or `end`. This is the thin renderer half: subscribe, get a stop
 * function back, or `null` when no signal can ever come.
 *
 * Two ways to reach the shell, as for lib/cursorAway:
 *  - NATIVE: the preload bridge, in the app's own top-level document.
 *  - RELAY: an embedded instance pane (a remote crew's dashboard in a
 *    cross-origin iframe) has no preload. It asks its host frame with
 *    {@link SCROLL_TOUCH_WATCH_TYPE}; the host, which has the bridge, forwards
 *    each phase to the pane the user is looking at, addressed to that pane's
 *    exact origin (see InstancesViewport). The pane accepts a phase only from
 *    `window.parent`, from the loopback origin it asked, for THIS watch.
 *
 * Wheel events need no relay: a remote pane's own document receives the wheels
 * over it, and its own swipe reader reads them.
 *
 * A phase is a hint. The reader keeps its own gap and momentum detection, so a
 * host that never answers (a browser parent, an older app) changes nothing.
 */
import { isEmbeddedPane } from './embedded'
import { relayTargetOrigin } from './nativeNotify'

export type ScrollTouchPhase = 'begin' | 'end'
/** `at` is when the phase reached this page, on the `performance.now()` clock. */
export type ScrollTouchListener = (phase: ScrollTouchPhase, at: number) => void

/** Pane -> host: send me the phases while I am the pane on screen. */
export const SCROLL_TOUCH_WATCH_TYPE = 'mc-scroll-touch-watch'
/** Pane -> host: stop. */
export const SCROLL_TOUCH_CANCEL_TYPE = 'mc-scroll-touch-cancel'
/** Host -> pane: one phase, `{ id, phase }`. */
export const SCROLL_TOUCH_PHASE_TYPE = 'mc-scroll-touch'
export const SCROLL_TOUCH_VERSION = 1
/** The longest watch id either side accepts. */
export const SCROLL_TOUCH_ID_MAX = 64

/** Narrow an untrusted value to a phase. */
export function asScrollTouchPhase(v: unknown): ScrollTouchPhase | null {
  return v === 'begin' || v === 'end' ? v : null
}

/**
 * The preload bridge alone, no relay. What the HOST uses to forward phases to a
 * pane: a host is top-level, so it must never take the postMessage path.
 */
export function subscribeScrollTouchNative(cb: ScrollTouchListener): (() => void) | null {
  const on = typeof window !== 'undefined' ? window.electronAPI?.onScrollTouch : undefined
  if (typeof on !== 'function') return null
  try {
    const stop = on((raw) => {
      const phase = asScrollTouchPhase(raw)
      if (phase) cb(phase, performance.now())
    })
    return typeof stop === 'function' ? stop : () => {}
  } catch {
    return null
  }
}

let relaySeq = 0

/** Ask the host frame for the phases. Null when there is no host to ask. */
function subscribeViaHost(cb: ScrollTouchListener): (() => void) | null {
  if (!isEmbeddedPane()) return null
  // The hub's exact loopback origin, never '*'; null when the browser withholds it.
  const target = relayTargetOrigin()
  if (target === null) return null
  const parent = window.parent
  const id = `${Date.now().toString(36)}-${(relaySeq += 1)}`
  let live = true
  function onMessage(e: MessageEvent) {
    // Only our own host frame, from the origin we addressed, about THIS watch.
    if (!live || e.source !== parent || e.origin !== target) return
    const d = e.data as { type?: unknown; v?: unknown; id?: unknown; phase?: unknown } | null
    if (!d || typeof d !== 'object' || d.type !== SCROLL_TOUCH_PHASE_TYPE) return
    if (d.v !== SCROLL_TOUCH_VERSION || d.id !== id) return
    const phase = asScrollTouchPhase(d.phase)
    if (phase) cb(phase, performance.now())
  }
  window.addEventListener('message', onMessage)
  try {
    parent.postMessage({ type: SCROLL_TOUCH_WATCH_TYPE, v: SCROLL_TOUCH_VERSION, id }, target)
  } catch {
    window.removeEventListener('message', onMessage)
    return null
  }
  return () => {
    if (!live) return
    live = false
    window.removeEventListener('message', onMessage)
    try {
      parent.postMessage({ type: SCROLL_TOUCH_CANCEL_TYPE, v: SCROLL_TOUCH_VERSION, id }, target)
    } catch {
      /* host gone: nothing left to disarm */
    }
  }
}

/**
 * Hear the finger phases. Returns a stop function, or null when this page can
 * never hear them (a plain browser tab, a shell older than the feature). Even a
 * non-null result may stay silent: off macOS, or under a host that ignores the
 * request. Callers treat "no phase heard yet" as "no signal".
 */
export function subscribeScrollTouch(cb: ScrollTouchListener): (() => void) | null {
  return subscribeScrollTouchNative(cb) ?? subscribeViaHost(cb)
}
