import { isChatPageSurface } from '../utils/channelOrigin'

/**
 * The sessions this tab closed, newest last — what ⌘⇧T / Ctrl+Shift+T
 * (`reopen-chat`, #18969) walks back through, the way a browser reopens its
 * closed tabs.
 *
 * Only a USER close is recorded (the ⌘W chord, the sidebar ✕, the session
 * menus — see {@link rememberClosedSession}'s callers). A swap that retires one
 * tab for another (a memory-mode switch, resuming a history row into the
 * current tab), a bulk cleanup and an app tearing down its own agent session
 * all close slots too, and reopening one of those would resurrect something the
 * user never closed.
 *
 * Deliberately in memory and per tab: it is an undo for "I closed that by
 * accident", not a second History. A reload clears it, and History still finds
 * anything older.
 */
export interface ClosedSessionRef {
  /** Bare dashboard slot key, used (with `historyKey`) only to decide whether the tab is already open. */
  key: string
  /** Persisted transcript stem, passed to the History resume endpoint; also what a reopened slot carries as `history_key`. */
  historyKey: string
  title: string
}

/** Enough for a run of accidental closes; History covers anything older. */
export const MAX_RECENTLY_CLOSED = 20

let closed: ClosedSessionRef[] = []

/** The fields of a dashboard slot this module reads. */
export interface ClosableSlot {
  key: string
  /** The backend's `slot_history_key`: the transcript stem History resumes. */
  history_key?: string
  title?: string
  mode?: string
  surface?: string
}

/**
 * Record a session the user just closed. Ignored when it could not be reopened
 * from here: a session on a surface the chat page cannot show (a crew window,
 * an app's own surface) would "reopen" into a notice that it cannot be
 * displayed, and a slot without `history_key` names no transcript to resume.
 * The backend that serves this page sends `history_key` on every slot, so the
 * key is never guessed from the slot's other fields.
 *
 * Incognito and temporary sessions ARE recorded: their transcript is kept in
 * History for the user to reopen (docs/decisions/2026-09-25-incognito-and-
 * temporary-chats-keep-their-transcript.md), and a reopen record stays in this
 * tab's memory rather than becoming a derived artifact.
 *
 * A key already on the stack moves to the top, so a session closed, reopened
 * and closed again is reopened once, not twice.
 */
export function rememberClosedSession(slot: ClosableSlot | undefined): void {
  if (!slot?.key || !slot.history_key) return
  if (!isChatPageSurface(slot.surface ?? slot.mode ?? '')) return
  closed = closed.filter(r => r.key !== slot.key)
  closed.push({ key: slot.key, historyKey: slot.history_key, title: slot.title || slot.key })
  if (closed.length > MAX_RECENTLY_CLOSED) closed = closed.slice(-MAX_RECENTLY_CLOSED)
}

/**
 * Put a popped entry back on top unchanged, for a reopen that failed. The entry
 * already carries its transcript stem, so it is not re-derived from a slot.
 */
export function restoreClosedSession(ref: ClosedSessionRef): void {
  closed = closed.filter(r => r.key !== ref.key)
  closed.push(ref)
  if (closed.length > MAX_RECENTLY_CLOSED) closed = closed.slice(-MAX_RECENTLY_CLOSED)
}

/**
 * Whether a recorded close is open again in some slot. Given the whole entry,
 * not just its key: the History resume publishes under
 * `_normalize_slot_key(history_key)`, so a slot whose transcript stem is not
 * `dashboard_<key>` (a channel-linked `chat-2`) comes back under a different
 * key than the one it was closed with, and only its `history_key` still
 * matches the entry.
 */
export type IsClosedSessionOpen = (ref: ClosedSessionRef) => boolean

/**
 * The open-slot check the shortcut uses: open when a slot has the entry's key
 * OR carries the entry's transcript stem as its `history_key`.
 */
export function isClosedSessionOpenIn(slots: readonly ClosableSlot[]): IsClosedSessionOpen {
  return ref => slots.some(s => s.key === ref.key || s.history_key === ref.historyKey)
}

/**
 * Pop the most recently closed session that is not open again. A session the
 * user already reopened another way (History, the palette, a link) is skipped
 * and dropped, so the next press reaches the session closed before it rather
 * than switching to a tab that is already there.
 */
export function takeRecentlyClosedSession(isOpen: IsClosedSessionOpen): ClosedSessionRef | null {
  while (closed.length) {
    const ref = closed.pop()!
    if (!isOpen(ref)) return ref
  }
  return null
}

/**
 * Whether a press would reopen anything, without consuming the entry -- so
 * the caller can skip the leave-guard ask when there is nothing to reopen.
 */
export function hasRecentlyClosedSession(isOpen: IsClosedSessionOpen): boolean {
  return closed.some(r => !isOpen(r))
}

/** Test seam: forget every recorded close. */
export function resetRecentlyClosedSessions(): void {
  closed = []
}
