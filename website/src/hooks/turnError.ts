/**
 * Which sessions' CURRENT turn has ended in a terminal error row.
 *
 * A turn that fails (sign-in required, a stuck runtime, a refused start) still
 * ends with an ordinary `chat_done`, so the frame alone cannot tell a failure
 * from a routine finish. The "mute sessions it opens" rule silences a routine
 * finish; a failure stalls the worker the same way a silenced approval would,
 * so it must still reach the user. This record is what lets `chat_done` tell
 * the two apart.
 *
 * Decided by the row's role and its structural retry tag, never by its text: a
 * transient-retry notice is also role `error`, but the runner has already
 * queued the turn that re-runs it, so nothing has stopped yet.
 *
 * Window-local, like the other attention state. Set on the row, taken and
 * cleared by the turn's `chat_done` so it never leaks into the next turn.
 */
import type { ChatMessage } from '../types'
import { isRetryNotice } from '../lib/retryNotice'

const erroredTurns = new Set<string>()

/** Whether a live `chat_message` row is a terminal turn error: role `error`
 *  without the transient-retry tag (on `kind` live, or `meta.kind`). */
export function isTerminalErrorRow(row: { role?: string; kind?: string; meta?: unknown }): boolean {
  return row.role === 'error' && !isRetryNotice(row as ChatMessage)
}

/** Record that *slotKey*'s running turn produced a terminal error row. */
export function noteTurnErrorRow(slotKey: string, row: { role?: string; kind?: string; meta?: unknown }): void {
  if (isTerminalErrorRow(row)) erroredTurns.add(slotKey)
}

/** Whether *slotKey*'s finishing turn produced a terminal error row; clears
 *  the record so the next turn starts clean. */
export function takeTurnErrored(slotKey: string): boolean {
  return erroredTurns.delete(slotKey)
}

export function _resetTurnErrorsForTest(): void {
  erroredTurns.clear()
}
