/**
 * "Has the cursor moved far enough away?" for an edge-revealed overlay.
 *
 * The renderer receives no mouse events once the pointer crosses a window edge,
 * so distance from the window is knowable only in the Electron main process
 * (`screen.getCursorScreenPoint()` against `BrowserWindow.getBounds()`). This is
 * the thin renderer half of that: hand it a callback, get a stop function back.
 *
 * Two ways to reach the main process:
 *  - NATIVE: the preload bridge, in the desktop app's own top-level document.
 *  - RELAY: an embedded instance pane (a cross-origin iframe of another
 *    gateway's dashboard) has no preload, so it asks its host frame over
 *    postMessage, and the host — which does have the bridge — watches on its
 *    behalf and forwards the one answer back (see InstancesViewport). A host
 *    that never answers (an older host, a plain browser parent, a background
 *    pane) leaves the overlay to blur, visibility and outside-click.
 *
 * Returns `null` when neither path exists — a plain browser tab, or a desktop
 * shell older than the feature. The caller then dismisses on nothing but blur,
 * visibility and outside-click.
 */
import { isEmbeddedPane } from './embedded'
import { relayTargetOrigin } from './nativeNotify'

/** `true` = far enough away, dismiss. `false` = came back inside instead. */
export type CursorAwayResult = (away: boolean) => void

/** Pane -> host: start watching on my behalf. */
export const CURSOR_AWAY_WATCH_TYPE = 'mc-cursor-away-watch'
/** Pane -> host: the answer no longer matters, disarm. */
export const CURSOR_AWAY_CANCEL_TYPE = 'mc-cursor-away-cancel'
/** Host -> pane: the one answer, `{ away: boolean }`. */
export const CURSOR_AWAY_RESULT_TYPE = 'mc-cursor-away'
export const CURSOR_AWAY_VERSION = 1

/**
 * The preload bridge alone, no relay. What the HOST uses to watch for a pane:
 * relaying a relayed request onward would be meaningless (the host is
 * top-level), so it must never take the postMessage path.
 */
export function watchCursorAwayNative(onResult: CursorAwayResult): (() => void) | null {
  const watch = window.electronAPI?.watchCursorAway
  if (typeof watch !== 'function') return null
  let done = false
  let stop: (() => void) | null = null
  const finish = (away: boolean) => {
    if (done) return
    done = true
    // The main process stops polling the moment it reports, but the listener is
    // still ours to release.
    stop?.()
    stop = null
    onResult(away)
  }
  try {
    stop = watch(finish) ?? null
  } catch {
    // A bridge that throws is a bridge that will never answer; report no
    // bridge rather than holding a watch nothing will resolve.
    return null
  }
  return () => {
    done = true
    stop?.()
    stop = null
  }
}

let relaySeq = 0

/** Ask the host frame to watch. Null when there is no host to ask. */
function watchViaHost(onResult: CursorAwayResult): (() => void) | null {
  if (!isEmbeddedPane()) return null
  // The exact loopback origin of the embedding hub, never '*': the same target
  // the native-notification relay uses, and null when the browser withholds it.
  const target = relayTargetOrigin()
  if (target === null) return null
  const parent = window.parent
  const id = `${Date.now().toString(36)}-${(relaySeq += 1)}`
  let done = false

  const teardown = () => {
    done = true
    window.removeEventListener('message', onMessage)
  }
  const finish = (away: boolean) => {
    if (done) return
    teardown()
    onResult(away)
  }
  function onMessage(e: MessageEvent) {
    // Only our own host frame, speaking from the origin we addressed, about THIS
    // watch. Anything else — a sibling frame, a stale reply to an earlier watch —
    // is ignored rather than allowed to dismiss or pin the overlay.
    if (done || e.source !== parent || e.origin !== target) return
    const data = e.data as { type?: unknown; v?: unknown; id?: unknown; away?: unknown } | null
    if (!data || typeof data !== 'object' || data.v !== CURSOR_AWAY_VERSION || data.id !== id) return
    if (data.type === CURSOR_AWAY_RESULT_TYPE && typeof data.away === 'boolean') {
      finish(data.away)
    }
  }

  window.addEventListener('message', onMessage)
  try {
    parent.postMessage({ type: CURSOR_AWAY_WATCH_TYPE, v: CURSOR_AWAY_VERSION, id }, target)
  } catch {
    teardown()
    return null
  }
  return () => {
    if (done) return
    teardown()
    try {
      parent.postMessage({ type: CURSOR_AWAY_CANCEL_TYPE, v: CURSOR_AWAY_VERSION, id }, target)
    } catch {
      /* host gone — nothing left to disarm */
    }
  }
}

/**
 * Start watching. The callback fires AT MOST ONCE; call the returned stop
 * function when the answer stops mattering (the overlay closed, the pointer came
 * back), which also disarms the main-process poll — directly, or through the
 * host frame for an embedded pane.
 */
export function watchCursorAway(onResult: CursorAwayResult): (() => void) | null {
  return watchCursorAwayNative(onResult) ?? watchViaHost(onResult)
}
