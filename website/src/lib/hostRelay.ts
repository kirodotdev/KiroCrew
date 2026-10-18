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
