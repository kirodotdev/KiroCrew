/**
 * Opt-in native OS toast for "a background chat finished".
 *
 * The caller first checks conversation attention in `notificationEvent.ts`.
 * This helper applies the separate native-toast preference, permission, and
 * away checks. Unlike a chime, a toast persists in the OS notification centre
 * and names WHICH session needs the user, so it stays default-OFF and fires
 * only while the user is away from the window.
 *
 * The preference lives in localStorage rather than gateway config because it is
 * a per-device browser capability — one machine may have OS notifications muted
 * and another not — exactly like the sound settings it sits beside in
 * Settings > Notifications.
 */
import { useSyncExternalStore } from 'react'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import { isWindowAway } from './windowAway'
import { nativeNotificationPermitted } from '../lib/nativeNotify'

/** localStorage key holding the opt-in. Absent — or anything but `'1'` —
 *  means off, so a corrupt or half-written value degrades to the default
 *  rather than to a surprise toast. */
export const CHAT_COMPLETE_NOTIFY_KEY = 'mc-notify-chat-complete'

// Reads the hub's relayed opt-in off the host model EmbeddedHostBridge stores
// (`HostModel.chatCompleteNotify`); the bridge injects the read, as it does for
// `lib/hostRelay.ts`, so the value lives only in Redux.
let readHostOptIn: () => boolean = () => false

/** Install the reader for the hub's opt-in; `null` restores the default
 *  (`false`). EmbeddedHostBridge sets it while mounted. */
export function setChatCompleteHostReader(reader: (() => boolean) | null): void {
  readHostOptIn = reader ?? (() => false)
}

/** Whether the user opted in. Default OFF: a background-heavy user would
 *  otherwise get one toast per concurrent completion the first time they
 *  minimize the window. An explicit choice in this origin's storage wins;
 *  otherwise an embedded pane follows the hub's relayed opt-in. */
export function loadChatCompleteNotify(): boolean {
  const own = safeGetItem(CHAT_COMPLETE_NOTIFY_KEY)
  if (own === '1' || own === '0') return own === '1'
  return readHostOptIn()
}

const listeners = new Set<() => void>()

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

/** Re-read the opt-in in every subscriber. EmbeddedHostBridge calls it on each
 *  host model, which may carry a new hub value. */
export function noteChatCompleteChanged(): void {
  listeners.forEach(listener => listener())
}

/** The opt-in as React state: the Settings switch shows the effective value,
 *  and the hub re-sends its host model to every pane the moment it flips. */
export function useChatCompleteNotify(): boolean {
  return useSyncExternalStore(subscribe, loadChatCompleteNotify)
}

/**
 * Persist the opt-in and, when enabling, ask for the OS permission.
 *
 * The permission request is the load-bearing half. `useNativeNotification` is
 * the only other place that asks, and only when an unacked FEED notification
 * arrives — so a user who enables this toggle without ever having received a
 * feed note would sit at `Notification.permission === 'default'` and the
 * feature would be a silent no-op. Flipping the toggle IS the user gesture
 * browsers require for the prompt, so ask here.
 */
export function saveChatCompleteNotify(on: boolean): void {
  safeSetItem(CHAT_COMPLETE_NOTIFY_KEY, on ? '1' : '0')
  noteChatCompleteChanged()
  if (!on || typeof Notification === 'undefined') return
  if (Notification.permission !== 'default') return
  try {
    void Notification.requestPermission()
  } catch {
    /* unsupported platform (a callback-only implementation returns nothing) */
  }
}

/**
 * Whether a finished turn warrants a native toast.
 *
 * Policy, in the order the checks read: a real turn (slot-bearing) that is not
 * a reconnect catch-up replay — mirroring the chime's two suppressions, since
 * replayed completions must not toast-storm — for a user who opted in, on a
 * platform that can show a toast, while that user is away from the window.
 *
 * The capability checks (`Notification` present, permission granted) live here
 * rather than at the call site so the whole gate is one testable predicate; the
 * caller is left with the construction the platform may still refuse. In an
 * embedded instance pane the permission is denied by design and the parent
 * frame posts on the pane's behalf, so the capability check defers to
 * `nativeNotificationPermitted()` rather than reading the pane's own verdict.
 */
export function shouldNotifyOnChatComplete(opts: {
  slot: string | undefined | null
  reconnecting: boolean
}): boolean {
  if (!opts.slot || opts.reconnecting) return false
  if (!loadChatCompleteNotify()) return false
  if (!nativeNotificationPermitted()) return false
  // The shared "away" predicate (both axes: hidden, and visible-but-unfocused);
  // see windowAway.ts for why one axis is not enough.
  return isWindowAway()
}
