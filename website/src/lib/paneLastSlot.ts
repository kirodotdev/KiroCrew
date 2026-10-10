/**
 * Remember which chat a remote-crew pane last showed, across reconnects (#16009).
 *
 * The pane remembers its own chat in `mc-active-slot-chat`, but that key lives
 * in the PANE's origin, and the origin carries the tunnel's local port. A
 * reconnect usually lands on a new port, so the pane boots with empty storage
 * and opens the first chat in its list instead of the one the user was in.
 *
 * So the pane also tells the parent (the hub window, whose origin does not
 * change) each time its chat changes. The parent keeps one key per crew and
 * puts it back on the pane URL as `?lastSid=` when it loads the pane again.
 * The pane treats it as a soft hint: used only when that chat still exists,
 * never as a deep link, so a deleted chat falls back silently.
 *
 * Trust: the parent accepts the relay only through its existing origin gate
 * (`resolveTunnelOrigin`), and both sides shape-check the key. The key is a
 * chat id, not a secret.
 */
import { safeSetItem } from '../utils/safeStorage'
import { relayTargetOrigin } from './nativeNotify'

export const PANE_ACTIVE_SLOT_TYPE = 'mc-pane-active-slot'
export const PANE_SLOT_HINT_PARAM = 'lastSid'
const STORE_KEY = 'mc-pane-last-slot'

/** A chat key is a short id: letters, digits and `._:-` only. */
export function isPaneSlotKey(v: unknown): v is string {
  return typeof v === 'string' && /^[A-Za-z0-9._:-]{1,128}$/.test(v)
}

function readAll(): Record<string, string> {
  try {
    const raw = JSON.parse(localStorage.getItem(STORE_KEY) ?? '{}') as unknown
    return raw && typeof raw === 'object' && !Array.isArray(raw) ? (raw as Record<string, string>) : {}
  } catch {
    return {}
  }
}

/** Parent side: the chat crew `instanceId` last showed, or null. */
export function readPaneSlot(instanceId: string): string | null {
  const v = readAll()[instanceId]
  return isPaneSlotKey(v) ? v : null
}

/** Parent side: remember the chat crew `instanceId` now shows. */
export function rememberPaneSlot(instanceId: string, key: unknown): void {
  if (!isPaneSlotKey(key)) return
  const all = readAll()
  if (all[instanceId] === key) return
  all[instanceId] = key
  safeSetItem(STORE_KEY, JSON.stringify(all))
}

/** Pane side: tell the parent hub which chat this pane now shows. */
export function relayActiveSlotToParent(key: string): void {
  // Only to the exact loopback hub origin; never '*' (see relayTargetOrigin).
  const target = relayTargetOrigin()
  if (!target) return
  try {
    window.parent.postMessage({ source: 'kirocrew', type: PANE_ACTIVE_SLOT_TYPE, key }, target)
  } catch { /* a relay must never break a state update */ }
}
