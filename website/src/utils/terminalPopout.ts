import { useSyncExternalStore } from 'react'
import {
  createPopoutController,
  applyMessage,
  pruneStale,
  HEARTBEAT_MS,
  STALE_MS,
  type PopoutMap,
  type PopoutMsg,
} from './popoutController'

/**
 * Cross-window coordination for the popped-out terminal panel.
 *
 * "Pop out" moves the app-wide docked terminal panel (ALL its tabs, as one
 * unit — mirroring how OS terminals detach a whole tabbed window) into a
 * dedicated same-origin browser window at `/popout/terminal`. The generic
 * coordination engine lives in `popoutController` (BroadcastChannel +
 * visibility-aware heartbeat + window-handle dedupe + popup-blocker
 * observability); this module specializes it for the terminal panel.
 *
 * Unlike chat/artifact popouts — keyed per session/slug — the terminal panel
 * is a singleton, so a single fixed entity id (`TERMINAL_POPOUT_ID`) is used.
 * Tab membership itself is NOT coordinated here: the bottom-terminal store is
 * localStorage-persisted, and `useBottomTerminal` subscribes to cross-window
 * `storage` events, so both windows always agree on the tab list. What this
 * channel adds is liveness (is a popout window open right now?) and control
 * (focus it / bring it back).
 *
 * PTY ownership: exactly one window holds a session's WebSocket at a time —
 * the backend replaces the socket on reconnect (replaying its scrollback ring
 * buffer), so the handoff is: the detaching side disposes its local WS +
 * xterm (keeping the PTY alive server-side) and the other window connects
 * fresh.
 */

export const TERMINAL_POPOUT_CHANNEL = 'kirocrew-terminal-popout'

/** The singleton entity id for the (one) terminal panel popout. */
export const TERMINAL_POPOUT_ID = 'terminal-panel'

export { HEARTBEAT_MS, STALE_MS, applyMessage, pruneStale }
export type { PopoutMap, PopoutMsg }

/** Stable `window.open` name — dedupes to a single terminal popout window. */
export function popoutWindowName(): string {
  return 'mc-popout-terminal'
}

/** Build the popout URL for the terminal panel window. */
export function buildPopoutUrl(): string {
  return `${window.location.origin}/popout/terminal`
}

const controller = createPopoutController({
  channelName: TERMINAL_POPOUT_CHANNEL,
  logLabel: 'terminalPopout',
  buildUrl: buildPopoutUrl,
  windowName: popoutWindowName,
  // returnSelfToMain fallback: a deep-linked popout with no script opener
  // can't close itself, so it becomes a main dashboard view instead.
  mainViewUrl: () => '/',
})

/** Subscribe a main-window listener (for useSyncExternalStore). Starts the heartbeat lazily. */
export const subscribe = controller.subscribe
/** Current popout-liveness set (contains TERMINAL_POPOUT_ID while a popout window is alive). */
export const getSnapshot = controller.getSnapshot
/** Open (or focus, if already open) the terminal panel in its own browser window. */
export function openPopout(): void { controller.openPopout(TERMINAL_POPOUT_ID) }
/**
 * True when the terminal popout is (optimistically) live. Synchronously true
 * right after a SUCCESSFUL `openPopout()` — the controller only marks the map
 * when `window.open` returned a handle, so callers can distinguish a vetoed
 * popup (popup blocker) from a real open without waiting for the heartbeat.
 */
export function isPopoutOpen(): boolean { return controller.getSnapshot().has(TERMINAL_POPOUT_ID) }
/** Focus the terminal popout window (direct handle, else ask it to focus itself). */
export function focusPopout(): void { controller.focusPopout(TERMINAL_POPOUT_ID) }
/** Close the terminal popout window and drop it from the map (caller re-docks the panel). */
export function bringBack(): void { controller.bringBack(TERMINAL_POPOUT_ID) }
/** True when THIS window is the live terminal popout. */
export function isSelfPopout(): boolean { return controller.isSelfPopout(TERMINAL_POPOUT_ID) }
/**
 * Return THIS popout window's panel to the main dashboard: focus the opener
 * and close; when the close is refused (no script opener), navigate this
 * window to the main dashboard instead.
 */
export const returnSelfToMain = controller.returnSelfToMain
/** Register THIS window as the live terminal popout (responder role). Returns cleanup. */
export function registerPopout(): () => void {
  const cleanup = controller.registerPopout(TERMINAL_POPOUT_ID)
  // localStorage liveness beacon, alongside the BroadcastChannel presence.
  // The channel handshake takes up to one heartbeat round-trip — a freshly
  // RELOADED main window would mount the docked panel in that gap, steal the
  // popout's PTY sockets, and then have its releaser close them (the orphan
  // reaper would eventually kill the PTYs). The beacon is readable
  // SYNCHRONOUSLY at main-window boot, closing that gap.
  writeBeacon()
  const beat = window.setInterval(writeBeacon, BEACON_INTERVAL_MS)
  const clear = () => {
    window.clearInterval(beat)
    try { localStorage.removeItem(BEACON_KEY) } catch { /* locked storage */ }
  }
  window.addEventListener('pagehide', clear)
  return () => {
    window.removeEventListener('pagehide', clear)
    clear()
    cleanup()
  }
}
const BEACON_KEY = 'mc-terminal-popout-alive'
const BEACON_INTERVAL_MS = 5_000
/** Beacon older than this is a crashed/killed popout — ignore it. */
const BEACON_TTL_MS = 15_000
function writeBeacon(): void {
  try { localStorage.setItem(BEACON_KEY, String(Date.now())) } catch { /* quota / locked storage */ }
}
/**
 * True when a live popout's beacon is present and fresh. Synchronous — safe to
 * call during a main window's first render, BEFORE the BroadcastChannel
 * heartbeat handshake has completed.
 */
export function hasFreshBeacon(): boolean {
  try {
    const raw = localStorage.getItem(BEACON_KEY)
    if (!raw) return false
    const ts = Number(raw)
    return Number.isFinite(ts) && Date.now() - ts <= BEACON_TTL_MS
  } catch {
    return false
  }
}
/** React hook: true while a terminal popout window is alive somewhere.
 *
 *  Union of two liveness sources: the BroadcastChannel map (event-driven,
 *  instant open/close signals) and the localStorage beacon (synchronously
 *  correct across a main-window reload). The beacon side re-evaluates on
 *  `storage` events (each popout heartbeat fires one) and expires via TTL,
 *  so a crashed popout still re-docks the panel within BEACON_TTL_MS. */
function getPoppedOutSnapshot(): boolean {
  return controller.getSnapshot().has(TERMINAL_POPOUT_ID) || hasFreshBeacon()
}
function subscribePoppedOut(cb: () => void): () => void {
  const unsubMap = controller.subscribe(cb)
  const onStorage = (e: StorageEvent) => { if (e.key === BEACON_KEY) cb() }
  window.addEventListener('storage', onStorage)
  // TTL expiry has no event — poll at the beacon cadence so a crashed
  // popout's stale beacon flips this hook false without user action.
  const tick = window.setInterval(cb, BEACON_INTERVAL_MS)
  return () => {
    unsubMap()
    window.removeEventListener('storage', onStorage)
    window.clearInterval(tick)
  }
}
export function useTerminalPoppedOut(): boolean {
  return useSyncExternalStore(subscribePoppedOut, getPoppedOutSnapshot, getPoppedOutSnapshot)
}
/** Test-only: swap the navigation sink (jsdom can't redefine window.location). */
export const __setNavigateForTests = controller.__setNavigateForTests
/** Test-only: reset all module state between cases. */
export const __resetForTests = controller.__resetForTests
