import { isEmbeddedPane } from './embedded'
import { isEmbeddableHubOrigin } from './tunnelOrigin'

// The origin the hub last sent this pane its model from, or null.
let hostOrigin: string | null = null

/**
 * Record where the hub's model came from. Called by EmbeddedHostBridge for every
 * model it accepts from `window.parent`; `origin` is that message's
 * `event.origin`, written by the browser, so later relays go back to exactly the
 * page that spoke. It must be a loopback origin a pane can embed under
 * (`isEmbeddableHubOrigin`); anything else clears it.
 */
export function noteHostOrigin(origin: string | null): void {
  hostOrigin = typeof origin === 'string' && isEmbeddableHubOrigin(origin) ? origin : null
}

// Reads the stored host model's `electron` field. This module stays store-free
// (the chat composer reaches it through the pop-out controller), so
// EmbeddedHostBridge injects the read instead of this file importing the store.
// The value itself lives only in Redux.
let readHostElectron: () => boolean = () => false

/**
 * Install the reader `isDesktopAppPane` uses for the host model's `electron`
 * field. EmbeddedHostBridge sets it while mounted; `null` restores the default
 * (`false`).
 */
export function setHostElectronReader(reader: (() => boolean) | null): void {
  readHostElectron = reader ?? (() => false)
}

/**
 * True when this dashboard is a crew's pane inside the desktop app, read off
 * the `electron` field of the host model EmbeddedHostBridge stores. The pane is
 * served from the crew's tunnel port, so its origin is never the window's own;
 * the desktop window-open handler sends such a `window.open` to the system
 * browser and returns no window. Callers that would open a same-origin window
 * use this to refuse up front with the real reason.
 */
export function isDesktopAppPane(): boolean {
  return readHostElectron() && isEmbeddedPane()
}

/**
 * Pane side: post `payload` up to the embedding Instances hub.
 *
 * Sent only to the hub's exact origin (see `noteHostOrigin`), never `'*'`, so a
 * page that frames this dashboard but is not the hub never receives it. When
 * that origin is unknown -- not embedded, no model yet, a non-loopback sender --
 * nothing is sent. Returns whether it posted. Never throws: a relay must not
 * break the click or key handler that calls it.
 */
export function postToHost(payload: Record<string, unknown>): boolean {
  const target = hostOrigin
  if (target === null || !isEmbeddedPane()) return false
  try {
    window.parent.postMessage(payload, target)
    return true
  } catch {
    return false
  }
}
