import type { Notification } from '../types'

/** Shared contract between useWebSocket.ts (dispatcher) and useNotificationSound.ts (listener). */
export const MC_NOTIFICATION_EVENT = 'mc-notification' as const
export const MC_SOUND_SETTINGS_CHANGED_EVENT = 'mc-notification-sound-changed' as const

export interface McNotificationDetail {
  kind?: string
}

/**
 * Fire the `mc-notification` DOM event for a sound `kind`. Centralizes the
 * dispatch + defensive try/catch that three websocket call sites (feed, approval,
 * turn-done) previously duplicated inline, so the `CustomEvent` wiring and its
 * error handling live in one place. Swallows listener errors (a broken listener
 * must not break WS handling) and logs once here.
 */
export function dispatchMcNotification(kind: string): void {
  try {
    const detail: McNotificationDetail = { kind }
    window.dispatchEvent(new CustomEvent(MC_NOTIFICATION_EVENT, { detail }))
  } catch (err) {
    // Last-resort surface for a broken notification listener; no app logger on this DOM-event path.
    // eslint-disable-next-line no-console
    console.warn('mc-notification listener error', err)
  }
}

/**
 * Sound kind for a conversation handing the floor back to the user. The
 * persisted 'turn' key preserves per-category preferences. This sound-only
 * event adds no notification-feed record, toast, or badge.
 */
export const TURN_DONE_KIND = 'turn' as const

/**
 * Sound kind for a tool approval or explicit question. Synthesized by the
 * websocket layer when the agent needs a user decision. Uses a distinct preset
 * so the user can distinguish "needs my action" from "finished".
 */
export const APPROVAL_KIND = 'approval' as const

/**
 * A turn sound means the conversation has stopped or needs an answer, not
 * merely that one model turn returned. Continuation comes from the terminal
 * frame, with live activity selectors as the fallback for older frames.
 * Reconnect replay and slot-less frames never request audio.
 */
export function shouldChimeOnTurnDone(opts: {
  slot: string | undefined | null
  reconnecting: boolean
  continuing?: boolean
  needsInput?: boolean
}): boolean {
  return !!opts.slot && !opts.reconnecting && (!!opts.needsInput || !opts.continuing)
}

/**
 * A feed `notification` frame the socket received LIVE — after this tab
 * mounted, outside a reconnect catch-up replay. The in-app banner
 * (`components/notifications/NotificationBanner.tsx`) listens for this, not
 * for the Redux `addNotification` action, because the store also fills from
 * the boot `fetchNotifications` snapshot and from replays, and a banner for a
 * note the user already has in the bell would announce history as news.
 * The dispatcher (useWebSocket) owns the replay half of that rule — it is the
 * only place that knows the socket is catching up; the listener owns the
 * silenced/passive/current-view half.
 */
export const MC_LIVE_NOTIFICATION_EVENT = 'mc-live-notification' as const

export interface McLiveNotificationDetail {
  note: Notification
}

export function dispatchLiveNotification(note: Notification): void {
  try {
    const detail: McLiveNotificationDetail = { note }
    window.dispatchEvent(new CustomEvent(MC_LIVE_NOTIFICATION_EVENT, { detail }))
  } catch (err) {
    // Same last-resort surface as dispatchMcNotification: a broken banner
    // listener must not take the socket handler down with it.
    // eslint-disable-next-line no-console
    console.warn('mc-live-notification listener error', err)
  }
}
