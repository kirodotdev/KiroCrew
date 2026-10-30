/** Opening a session a server route has just created and answered with its key. */
import type { AppDispatch } from '../index'
import { listForkedSlot } from './lifecycle'
import { switchSlot } from './slotSwitch'

/** Open a session that a server route has just created or re-created and
 *  answered with its key alone: a task run's review chat (`/to-chat`), or a
 *  cron job's tab.
 *
 *  The key is listed as a membership add before the switch, the same add a
 *  fork or an import makes. A full `slots` frame the gateway serialized before
 *  the session existed can still reach the tab after the switch, and a
 *  whole-list replace drops any key that was not stamped as an add. The chat
 *  page then clears the selection and returns the user to the chat they came
 *  from. An already-listed key keeps its row, and the add only re-stamps it.
 *
 *  The switch keeps its target on a 404. The server listed the session before
 *  it answered, so a missing slot here is a create/fetch race, not a gone
 *  session to unwind from. */
export function openCreatedSlot(dispatch: AppDispatch, key: string) {
  dispatch(listForkedSlot({ key }))
  return dispatch(switchSlot({ key, keepTargetOnMissing: true }))
}
