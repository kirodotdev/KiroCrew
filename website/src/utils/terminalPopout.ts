import { useCallback, useSyncExternalStore } from 'react'
import { refreshTerminalState } from '../hooks/useBottomTerminal'
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
 * Terminal windows are scoped to the chat session that opened them. Presence
 * and controls use that same scope; selecting another chat never transfers
 * sockets or tab membership. null retains the legacy unassigned panel.
 * useBottomTerminal owns cross-window tab synchronization. The detaching
 * caller must release local connections before the popout connects, leaving
 * server PTYs alive for scrollback replay.
 */

export const TERMINAL_POPOUT_CHANNEL = 'kirocrew-terminal-popout'

/** Legacy unassigned panel identity; session panels append their scope. */
export const TERMINAL_POPOUT_ID = 'terminal-panel'

export { HEARTBEAT_MS, STALE_MS, applyMessage, pruneStale }
export type { PopoutMap, PopoutMsg }

function entityId(scope: string | null): string {
  return scope === null ? TERMINAL_POPOUT_ID : `${TERMINAL_POPOUT_ID}:${scope}`
}
function entityScope(id: string): string | null {
  return id === TERMINAL_POPOUT_ID ? null : id.slice(TERMINAL_POPOUT_ID.length + 1)
}

/** Stable and collision-free, including scope keys containing punctuation. */
export function popoutWindowName(scope: string | null): string {
  return scope === null ? 'mc-popout-terminal' : `mc-popout-terminal-${encodeURIComponent(scope)}`
}

export function buildPopoutUrl(scope: string | null): string {
  const query = scope === null ? '' : `?${new URLSearchParams({ sid: scope })}`
  return `${window.location.origin}/popout/terminal${query}`
}

let prepareReturn: (() => void) | undefined
const controller = createPopoutController({
  channelName: TERMINAL_POPOUT_CHANNEL,
  logLabel: 'terminalPopout',
  buildUrl: id => buildPopoutUrl(entityScope(id)),
  windowName: id => popoutWindowName(entityScope(id)),
  // returnSelfToMain fallback: a deep-linked popout with no script opener
  // can't close itself, so it becomes a main dashboard view instead.
  mainViewUrl: id => {
    const scope = id === null ? null : entityScope(id)
    return scope === null ? '/' : `/chat?${new URLSearchParams({ sid: scope })}`
  },
  navFallback: () => controller.returnSelfToMain(),
  waitForClose: true,
  beforeReturn: () => prepareReturn?.(),
})

/** Subscribe a main-window listener (for useSyncExternalStore). Starts the heartbeat lazily. */
export const subscribe = controller.subscribe
/** Current set of terminal panel entity ids, each carrying its originating scope. */
export const getSnapshot = controller.getSnapshot
/** Open (or focus, if already open) the terminal panel in its own browser window. */
export function openPopout(scope: string | null): void { controller.openPopout(entityId(scope)) }
/**
 * True when the terminal popout is (optimistically) live. Synchronously true
 * right after a SUCCESSFUL `openPopout()` — the controller only marks the map
 * when `window.open` returned a handle, so callers can distinguish a vetoed
 * popup (popup blocker) from a real open without waiting for the heartbeat.
 */
export function isPopoutOpen(scope: string | null): boolean { return controller.getSnapshot().has(entityId(scope)) }
/** Focus the terminal popout window (direct handle, else ask it to focus itself). */
export function focusPopout(scope: string | null): void { controller.focusPopout(entityId(scope)) }
/** Request return; presence remains until the popup releases its sockets and closes. */
export function bringBack(scope: string | null): void { controller.bringBack(entityId(scope)) }
/** True when THIS window is the live terminal popout. */
export function isSelfPopout(scope: string | null): boolean { return controller.isSelfPopout(entityId(scope)) }
/**
 * Explicit Return selects the originating chat and reveals its terminals.
 * Wait for the main window to receive the navigation and call bringBack;
 * closing here would abandon the asynchronous claim handshake.
 */
export function returnSelfToMain(scope: string | null): void {
  if (scope === null) controller.returnSelfToMain()
  else controller.forwardToMain({ path: '/chat', slotKey: scope })
}
/** Last-tab close exits without selecting a chat or reopening its empty panel. */
export const closeSelfToMain = controller.returnSelfToMain
export const setNavIntentHandler = controller.setNavIntentHandler
/** Register THIS window as the live terminal popout (responder role). Returns cleanup. */
export function registerPopout(scope: string | null, releaseConnections?: () => void): () => void {
  const cleanup = controller.registerPopout(entityId(scope))
  // localStorage liveness beacon, alongside the BroadcastChannel presence.
  // The channel handshake takes up to one heartbeat round-trip — a freshly
  // RELOADED main window would mount the docked panel in that gap, steal the
  // popout's PTY sockets, and then have its releaser close them (the orphan
  // reaper would eventually kill the PTYs). The beacon is readable
  // SYNCHRONOUSLY at main-window boot, closing that gap.
  const key = beaconKey(scope)
  const write = () => writeBeacon(key)
  write()
  const beat = window.setInterval(write, BEACON_INTERVAL_MS)
  const clear = () => {
    window.clearInterval(beat)
    try { localStorage.removeItem(key) } catch { /* locked storage */ }
  }
  const beforeReturn = () => {
    releaseConnections?.()
    clear()
  }
  prepareReturn = beforeReturn
  window.addEventListener('pagehide', clear)
  return () => {
    window.removeEventListener('pagehide', clear)
    clear()
    if (prepareReturn === beforeReturn) prepareReturn = undefined
    cleanup()
  }
}
const BEACON_KEY = 'mc-terminal-popout-alive'
const BEACON_INTERVAL_MS = 5_000
/** Beacon older than this is a crashed/killed popout — ignore it. */
const BEACON_TTL_MS = 15_000
function beaconKey(scope: string | null): string {
  return scope === null ? BEACON_KEY : `${BEACON_KEY}:${scope}`
}
function writeBeacon(key: string): void {
  try { localStorage.setItem(key, String(Date.now())) } catch { /* quota / locked storage */ }
}
/**
 * True when a live popout's beacon is present and fresh. Synchronous — safe to
 * call during a main window's first render, BEFORE the BroadcastChannel
 * heartbeat handshake has completed.
 */
export function hasFreshBeacon(scope: string | null): boolean {
  try {
    const raw = localStorage.getItem(beaconKey(scope))
    if (!raw?.trim()) return false
    const ts = Number(raw)
    const age = Date.now() - ts
    return Number.isFinite(age) && age >= 0 && age <= BEACON_TTL_MS
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
 *  so a crashed popout expires too. Re-docking then waits for canonical state
 *  refresh; a failed read must never reconnect cached deleted PTYs. */
interface ReturnGate {
  held: boolean
  live: boolean
  epoch: number
  pending: Promise<void> | null
  listeners: Set<() => void>
}
const returnGates = new Map<string | null, ReturnGate>()
function gateFor(scope: string | null): ReturnGate {
  let gate = returnGates.get(scope)
  if (!gate) {
    gate = { held: false, live: false, epoch: 0, pending: null, listeners: new Set() }
    returnGates.set(scope, gate)
  }
  return gate
}
function observePresence(scope: string | null, gate: ReturnGate): boolean {
  const live = controller.getSnapshot().has(entityId(scope)) || hasFreshBeacon(scope)
  if (live !== gate.live) { gate.live = live; gate.epoch++ }
  if (live) gate.held = true
  return live
}
function getPoppedOutSnapshot(scope: string | null): boolean {
  const gate = gateFor(scope)
  // Remember a detached render even if the window closes before subscription.
  observePresence(scope, gate)
  return gate.held
}
function reconcileReturn(scope: string | null, gate: ReturnGate): void {
  const live = observePresence(scope, gate)
  if (live || !gate.held || gate.pending) return
  const epoch = gate.epoch
  // Close announcements and storage notifications travel independently. Hold
  // the main host off until canonical state has been published; otherwise a
  // last-tab deletion can reconnect a stale cached PTY when its popup closes.
  gate.pending = refreshTerminalState(scope).then(ok => {
    if (returnGates.get(scope) !== gate) return
    observePresence(scope, gate)
    if (ok && !gate.live && gate.epoch === epoch) gate.held = false
  }).finally(() => {
    gate.pending = null
    if (returnGates.get(scope) !== gate) return
    for (const listener of gate.listeners) listener()
    if (gate.epoch !== epoch) reconcileReturn(scope, gate)
  })
}
function subscribePoppedOut(scope: string | null, cb: () => void): () => void {
  const gate = gateFor(scope)
  gate.listeners.add(cb)
  const update = () => {
    // Track A's reopen/close epoch even while the main UI is viewing B and
    // A's previous canonical refresh is still in flight.
    for (const [trackedScope, trackedGate] of returnGates) observePresence(trackedScope, trackedGate)
    reconcileReturn(scope, gate)
    cb()
  }
  const unsubMap = controller.subscribe(update)
  const onStorage = (e: StorageEvent) => {
    if (e.key === null || e.key === BEACON_KEY || e.key.startsWith(`${BEACON_KEY}:`)) update()
  }
  window.addEventListener('storage', onStorage)
  // TTL expiry has no event — poll at the beacon cadence so a crashed
  // popout's stale beacon flips this hook false without user action.
  const tick = window.setInterval(update, BEACON_INTERVAL_MS)
  update()
  return () => {
    gate.listeners.delete(cb)
    unsubMap()
    window.removeEventListener('storage', onStorage)
    window.clearInterval(tick)
  }
}
export function useTerminalPoppedOut(scope: string | null): boolean {
  const subscribeScope = useCallback((cb: () => void) => subscribePoppedOut(scope, cb), [scope])
  const snapshot = useCallback(() => getPoppedOutSnapshot(scope), [scope])
  return useSyncExternalStore(subscribeScope, snapshot, snapshot)
}
/** Test-only: swap the navigation sink (jsdom can't redefine window.location). */
export const __setNavigateForTests = controller.__setNavigateForTests
/** Test-only: reset all module state between cases. */
export function __resetForTests(): void {
  prepareReturn = undefined
  returnGates.clear()
  controller.__resetForTests()
}
