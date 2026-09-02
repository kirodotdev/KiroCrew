import { useSyncExternalStore } from 'react'
import { isEmbeddedPane } from '../lib/embedded'

/**
 * Focus mode — the dashboard's chrome (top bar + left nav rail) leaves the shell
 * grid and becomes two edge-triggered hover overlays, so the surface the user is
 * actually working in fills the window.
 *
 * DELIBERATELY NOT PERSISTED. This is a view state, not a preference: it belongs
 * to the window you are looking at right now and resets on reload. That is also
 * what lets it be ONE value across the whole app — the local dashboard and every
 * embedded remote pane agree, in both directions, because there is no per-origin
 * localStorage to diverge (a remote pane is a cross-origin iframe with its own
 * storage, so a persisted setting could never have been shared).
 *
 * Module-level rather than context, same shape as `useRailWidth`: the value has to
 * survive consumer remounts and be readable from a keydown handler, and every
 * surface that shows it (the top-bar toggle, the shell, the instances viewport)
 * mounts independently.
 */
let enabled = false
const listeners = new Set<() => void>()

/** Current state, read outside React (a test seam, like `__resetFocusMode`). */
export function focusModeEnabled(): boolean {
  return enabled
}

/**
 * Set focus mode for THIS document and notify every consumer.
 *
 * `echo` controls the cross-frame relay and exists to stop a message loop: a
 * user-driven toggle inside an embedded pane must travel up to the host (which
 * re-broadcasts it to its other panes), but the pane ADOPTING a relayed value
 * must not send it straight back. The setter is idempotent, so even a stray echo
 * terminates rather than ping-ponging.
 */
export function setFocusModeEnabled(on: boolean, { echo = true }: { echo?: boolean } = {}): void {
  if (enabled === on) return
  enabled = on
  if (echo && isEmbeddedPane()) {
    try {
      // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
      window.parent?.postMessage({ type: 'mc-set-focus-mode', v: 1, on }, '*')
    } catch {
      /* no parent / cross-origin restriction — the host's next broadcast reconciles */
    }
  }
  listeners.forEach(l => l())
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}

const getSnapshot = () => enabled

/** Subscribe to focus mode. One shared value; every consumer re-renders together. */
export function useFocusMode() {
  const value = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  return {
    enabled: value,
    toggle: () => setFocusModeEnabled(!enabled),
  }
}

/**
 * Inset (px) focus mode keeps at the window edges it reclaims.
 *
 * Matches the 8px the content row's own cards already carry at the BOTTOM (the
 * side panel's and nav rail's `mb-2`, the sessions drawer's `pb-2`). Those cards
 * have no top or left margin because the 42px top bar row and the 236px rail
 * column supplied that clearance, so collapsing both tracks to zero leaves them
 * flush against the window's top and left edges while still inset at the bottom.
 *
 * The TOP edge no longer needs a macOS exception: the native traffic lights are
 * hidden along with the header, so there is nothing left to reserve for.
 */
export const FOCUS_INSET = 8


/**
 * Whether the chrome is on screen RIGHT NOW, as one value for the whole window.
 *
 * Separate from `enabled` because the answer does not always come from this
 * document: when a remote pane fills the window it is the PANE that peeks its own
 * header, and the host is the only one that can act on it — the native traffic
 * lights and the injected drag bar are the host window's, and a cross-origin pane
 * has no preload to reach them. So the pane relays its state up (`mc-focus-chrome`)
 * and the host writes it here, while the local shell writes it when no pane is
 * active. One store, one writer per context, one place the IPC is sent from.
 */
let chromeVisible = true
const chromeListeners = new Set<() => void>()

/** Current chrome visibility, read outside React (a test seam). */
export function focusChromeVisible(): boolean {
  return chromeVisible
}

export function setFocusChromeVisible(on: boolean): void {
  if (chromeVisible === on) return
  chromeVisible = on
  chromeListeners.forEach(l => l())
}

function subscribeChrome(cb: () => void) {
  chromeListeners.add(cb)
  return () => { chromeListeners.delete(cb) }
}

const getChromeSnapshot = () => chromeVisible

export function useFocusChromeVisible() {
  return useSyncExternalStore(subscribeChrome, getChromeSnapshot, getChromeSnapshot)
}

/** Test seam: restore the module default between cases. */
export function __resetFocusMode() {
  enabled = false
  chromeVisible = true
  listeners.clear()
  chromeListeners.clear()
}
