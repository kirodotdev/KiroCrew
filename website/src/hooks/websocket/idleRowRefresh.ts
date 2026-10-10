/** The turn whose completion refresh an idle `slots` row already dispatched.
 *
 *  A live `slots` row that reports the active slot not running ends its turn
 *  without a `_done` (`useSlotListSync`). That end dispatches the same
 *  completion refresh `chat_done` dispatches, so the pane is idle AND
 *  re-hydrated. The `_done` that usually follows milliseconds later for the
 *  same turn must then not fetch the same transcript a second time: the slot
 *  list owner records the ended turn's wire identity here and the turn
 *  completion owner takes it. Shared by those two owners only. A new
 *  connection clears the records, which can only cost one extra refresh,
 *  never skip one.
 *
 *  Not the store's `ChatState.endedTurn`, which records that a turn ended:
 *  every idle row writes that one, including a row that found the pane
 *  already idle and dispatched no refresh, and the `_done` reducer writes it
 *  before the turn completion owner runs. Only this record says the refresh
 *  was dispatched. */
import type { TurnIdentityFields } from '../../types'
import { wireTurnIdentity } from '../../store/chatSlice'

type WireIdentity = NonNullable<ReturnType<typeof wireTurnIdentity>>

const idleRowRefreshes = new Map<string, WireIdentity>()

/** Remember that the idle row for `identity` dispatched `slot`'s completion
 *  refresh. A row without identity (an older gateway) leaves no record, so the
 *  `_done` that follows refreshes as before. */
export function recordIdleRowRefresh(slot: string, identity: WireIdentity | null): void {
  if (identity) idleRowRefreshes.set(slot, identity)
  else idleRowRefreshes.delete(slot)
}

/** Whether the idle `slots` row already dispatched the completion refresh for
 *  the turn this `chat_done` ends. Consumes the slot's record either way: the
 *  record answers exactly one `_done`. Only the same wire identity matches, so
 *  a record left by a lost `_done` cannot swallow the next turn's refresh, and
 *  an identity-less `_done` (an older gateway) always refreshes. */
export function takeIdleRowRefresh(slot: string, done: TurnIdentityFields): boolean {
  const recorded = idleRowRefreshes.get(slot)
  idleRowRefreshes.delete(slot)
  const identity = wireTurnIdentity(done)
  return !!recorded && !!identity && recorded.gen === identity.gen && recorded.turn === identity.turn
}

/** A `_done` the old socket never delivered will not arrive on the new one. */
export function clearIdleRowRefreshes(): void {
  idleRowRefreshes.clear()
}
