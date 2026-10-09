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

/** The rows a crewmate writes TO the user. A member DM thread is a
 *  conversation with a named colleague, so its unread dot means "they said
 *  something to you", never "they ran a tool": a crewmate working through a
 *  long patrol would otherwise light the Crewmates rail on every tool call and
 *  the user would open the thread to find nothing addressed to them. */
const MEMBER_MESSAGE_ROLES: ReadonlySet<string> = new Set(['assistant', 'permission'])

/** Whether a `chat_message` row badges a member DM thread (`slot.mode ===
 *  'member'`). Narrower than `chatMessageMarksUnread`, never wider: only an
 *  `assistant` or `permission` row qualifies, and the opt-in still applies on
 *  top (with it on, an `assistant` row stays quiet here too). */
export function memberThreadRowMarksUnread(role: string | undefined): boolean {
  return role !== undefined && MEMBER_MESSAGE_ROLES.has(role) && chatMessageMarksUnread(role)
}

/** The member threads whose CURRENT turn has delivered a row to the user.
 *  Window-local, like the other attention state: it decides only whether
 *  this window badges the `chat_done`. Set on any such row (on or off
 *  screen -- the user watching it arrive still means the turn spoke), taken
 *  and cleared by the turn's `chat_done`. */
const memberTurnsThatSpoke = new Set<string>()

/** Record that *slotKey*'s running turn delivered *role* to the user, when it
 *  is one of the rows a member thread badges on. */
export function noteMemberThreadRow(slotKey: string, role: string | undefined): void {
  if (role !== undefined && MEMBER_MESSAGE_ROLES.has(role)) memberTurnsThatSpoke.add(slotKey)
}

/** Whether *slotKey*'s finishing turn delivered a row to the user; clears
 *  the record so the next turn starts silent. A `chat_done` on a member
 *  thread badges only when this is true (or the turn paused for input): a
 *  patrol that only ran tools and ended quietly has nothing to show. */
export function takeMemberThreadSpoke(slotKey: string): boolean {
  return memberTurnsThatSpoke.delete(slotKey)
}

export function _resetMemberThreadTurnsForTest(): void {
  memberTurnsThatSpoke.clear()
}

/** The server reserves the `member-` key prefix for member DM slots
 *  (`slot_registry.py` refuses any other mode under it), so the prefix alone
 *  identifies a thread whose slot row has not yet reached `dashboard.slots`. */
const MEMBER_SLOT_KEY_PREFIX = 'member-'

/** Whether *slotKey* is a member DM thread: by the slot's `mode` when the
 *  slots list has it, else by the reserved key prefix. */
export function isMemberThreadSlot(slotKey: string, slots: ReadonlyArray<{ key: string; mode?: string }> | undefined): boolean {
  const entry = slots?.find(s => s.key === slotKey)
  return entry ? entry.mode === 'member' : slotKey.startsWith(MEMBER_SLOT_KEY_PREFIX)
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
