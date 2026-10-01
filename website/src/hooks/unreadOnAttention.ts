/**
 * Opt-in quieter unread badge: light a background session only when the agent
 * is done or waiting on the user, not on every message it writes.
 *
 * Per-device, in localStorage, beside the other Settings > Notifications
 * preferences. Absent or anything but `'1'` means OFF, so today's behaviour
 * (every message badges) stays the default.
 */
import { safeGetItem, safeSetItem } from '../utils/safeStorage'

export const UNREAD_ON_ATTENTION_KEY = 'mc-unread-on-attention'

export function loadUnreadOnAttention(): boolean {
  return safeGetItem(UNREAD_ON_ATTENTION_KEY) === '1'
}

export function saveUnreadOnAttention(on: boolean): boolean {
  return safeSetItem(UNREAD_ON_ATTENTION_KEY, on ? '1' : '0')
}

/** Whether a `chat_message` row badges its session. With the opt-in on, only
 *  a `permission` row does: the turn is parked on the user's approval. The
 *  finished turn (`chat_done`) and a question card badge on their own paths. */
export function chatMessageMarksUnread(role: string | undefined): boolean {
  return !loadUnreadOnAttention() || role === 'permission'
}

/** Roles the gateway never saves to disk (`_TRANSIENT_ROLES` in
 *  `dashboard/state.py`). A gateway restart drops these rows, so the slot's
 *  `last_ts` never again reaches their timestamps. */
const UNSAVED_ROLES: ReadonlySet<string> = new Set(['chunk', 'done', 'streaming', 'queued', 'permission'])

/** The unread watermark a `chat_message` row may record: its own server ts,
 *  unless the row is one the gateway never saves. A watermark taken from an
 *  unsaved row sits above every `last_ts` a restarted gateway can report, so
 *  no read could ever cover it and the badge could never be cleared. Returning
 *  undefined lets `markSlotUnread` fall back to the slot's `last_ts`. */
export function unreadWatermarkTs(role: string | undefined, ts: string | undefined): string | undefined {
  if (!ts || (role !== undefined && UNSAVED_ROLES.has(role))) return undefined
  return ts
}
