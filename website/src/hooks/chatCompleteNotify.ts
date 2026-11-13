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

/** localStorage key holding the shortest turn, in seconds, that earns a toast.
 *  Absent, `'0'` or anything that is not a whole number in range means every
 *  turn does. */
export const CHAT_COMPLETE_MIN_SECONDS_KEY = 'mc-notify-min-turn-seconds'

/** Upper bound for the threshold. A stored value past an hour is corrupt or
 *  hand-edited, and honoring it would silence the toast for good. */
export const CHAT_COMPLETE_MIN_SECONDS_MAX = 3600

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

/** The shortest turn, in whole seconds, that earns a toast. `0` (the default)
 *  means every turn. A value that is not a whole number from 0 to
 *  `CHAT_COMPLETE_MIN_SECONDS_MAX` reads as `0`, so a corrupt entry degrades to
 *  today's behavior rather than to a toast that never fires. */
export function loadChatCompleteMinSeconds(): number {
  const raw = safeGetItem(CHAT_COMPLETE_MIN_SECONDS_KEY)
  if (raw === null || !/^\d{1,4}$/.test(raw)) return 0
  const seconds = Number(raw)
  return seconds <= CHAT_COMPLETE_MIN_SECONDS_MAX ? seconds : 0
}

/** The threshold as React state, so the Settings select shows the stored value. */
export function useChatCompleteMinSeconds(): number {
  return useSyncExternalStore(subscribe, loadChatCompleteMinSeconds)
}

/** Persist the threshold; a value out of range or not whole is stored as `0`. */
export function saveChatCompleteMinSeconds(seconds: number): void {
  const whole = Number.isInteger(seconds) && seconds >= 0 && seconds <= CHAT_COMPLETE_MIN_SECONDS_MAX ? seconds : 0
  safeSetItem(CHAT_COMPLETE_MIN_SECONDS_KEY, String(whole))
  noteChatCompleteChanged()
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
 * platform that can show a toast, while that user is away from the window. When
 * the user chose a minimum turn length, a turn known to be shorter is skipped.
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
  /** Whole seconds the finished turn ran. Leave it out (or pass `null`) when
   *  the duration is unknown or the turn must be announced whatever its
   *  length, such as one that stopped on a question or an error. */
  turnSeconds?: number | null
}): boolean {
  if (!opts.slot || opts.reconnecting) return false
  if (!loadChatCompleteNotify()) return false
  const minSeconds = loadChatCompleteMinSeconds()
  if (minSeconds > 0 && typeof opts.turnSeconds === 'number' && opts.turnSeconds < minSeconds) return false
  if (!nativeNotificationPermitted()) return false
  // The shared "away" predicate (both axes: hidden, and visible-but-unfocused);
  // see windowAway.ts for why one axis is not enough.
  return isWindowAway()
}
