import { api } from '../../api/client'
import { indeterminate, sendTurn, type SendWire } from '../../chat-core/transport/sendTurn'
import { mintSendId } from '../../utils/sendDelivery'

/** The fetch seam for a Dashboard card's reply: the usual `POST /api/chat?ws=1`, plus
 *  `origin: 'dashboard_card'`, which the gateway checks again and which tags the turn. */
const cardSendWire: SendWire = async (payload, signal) => {
  try {
    return await api.sendChat(payload.message, payload.slot, undefined, signal, payload.meta, undefined, undefined, 'dashboard_card')
  } catch {
    // A raw rejection cannot tell "never left" from "accepted, then the connection
    // dropped before headers". This surface has no optimistic bubble to resend from, so
    // handing the text back could run the turn twice: say indeterminate (SendWire).
    throw indeterminate()
  }
}

/**
 * Send a reply a Dashboard-tab button offered into the crewmate's own thread.
 *
 * Resolves `'sent'` when the gateway took it (or may have: a late or unreadable
 * answer, or a dropped connection, is not a refusal, and filling the composer then
 * would invite a second send). `'off'` is the person's own choice: the Profile switch (`card_send_off`) is
 * off. `'refused'` is the gateway's other "not now" (a `card_*` code: the thread is
 * busy or waiting on an approval or a question, or a send went through moments ago).
 * `'failed'` is anything else the gateway answered: a server error, an expired
 * sign-in. For all three, the caller fills the composer and the person presses
 * send, which is what a button did before one-click sends existed. Only `'off'` is
 * not shown as an error, because nothing went wrong.
 */
export type CardSendOutcome = 'sent' | 'off' | 'refused' | 'failed'

export async function sendCardReply(slot: string, text: string): Promise<CardSendOutcome> {
  const receipt = await sendTurn({
    message: text,
    slot,
    // Echoed back by the gateway, so the sent row appears in this tab.
    meta: { sendId: mintSendId() },
    wire: cardSendWire,
  })
  if (receipt.status === 'transport-error') return 'failed'
  if (receipt.status === 'refused') {
    const code = (receipt.body as { code?: unknown }).code
    if (code === 'card_send_off') return 'off'
    return typeof code === 'string' && code.startsWith('card_') ? 'refused' : 'failed'
  }
  return 'sent'
}
