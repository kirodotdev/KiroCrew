/**
 * Dashboard edit mode's change request, sent as the person's own message to a
 * crewmate's thread (preview `dashboard_edit_mode`).
 *
 * Drawn in the open chat the moment it is sent, the way the chat composer draws a
 * send: an optimistic user row carrying a `sendId`, and the POST carries the same
 * id, so the gateway's user echo reconciles that row instead of adding a second.
 * The receipt then settles it:
 *  - dispatched or queued: confirmed. A crewmate chat draws no queue card, so a
 *    queued request keeps its row, as the composer's crewmate send does.
 *  - refused: nothing was sent, so the row is taken back; the box keeps the text.
 *  - any indeterminate answer: the row stays, marked unconfirmed, and the box says
 *    to check the chat before sending again.
 */
import { mintSendId, sendTurn } from '../../chat-core/transport/sendTurn'
import type { AppDispatch } from '../../store'
import { appendSlotMessage, confirmOptimisticSend, dropUnconfirmedRow, markSendUnconfirmed } from '../../store/chatSlice'
import { changeRequestOutcome, type ChangeRequestOutcome } from './dashboardEdit'

export async function sendChangeRequest(dispatch: AppDispatch, slot: string, text: string): Promise<ChangeRequestOutcome> {
  const sendId = mintSendId()
  dispatch(appendSlotMessage({
    slot,
    message: { role: 'user', content: text, cls: 'msg msg-u', ts: new Date().toISOString(), meta: { optimistic: true, sendId } },
  }))
  const receipt = await sendTurn({ slot, message: text, meta: { sendId } })
  const outcome = changeRequestOutcome(receipt.status)
  if (outcome === 'sent') dispatch(confirmOptimisticSend({ slot, sendId }))
  else if (outcome === 'refused') dispatch(dropUnconfirmedRow({ slot, sendId }))
  else dispatch(markSendUnconfirmed({ slot, sendId }))
  return outcome
}
