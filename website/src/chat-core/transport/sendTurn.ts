import { api } from '../../api/client'
import { confirmedDelivered, readSendReceipt, SendReceiptBody } from '../../utils/sendDelivery'

/** Stop waiting on a send's response. Reaching this bound means the request
 *  was received and only the reply is late: the turn is running and its output
 *  arrives over the WebSocket, not through this promise. It is NOT a failure
 *  signal, which is why the receipt below gives it its own status instead of
 *  folding it into the error shapes. */
export const SEND_ABORT_MS = 10_000

/**
 * Every outcome a send can have, normalized. This is the receipt contract that
 * used to be re-derived (differently) at each hand-rolled call site. Parsing
 * (`refused` vs `unknown` vs accepted) is `readSendReceipt`'s ruling; this
 * layer adds the outcomes a parse cannot see -- the abort deadline and the
 * transport reject -- and splits acceptance by what it means for
 * an optimistic bubble:
 *
 * - `dispatched`      -- the server took custody of the message as an IMMEDIATE
 *                        turn. This is the delivery receipt for an optimistic
 *                        bubble: no `chat_message` echo is coming for a
 *                        dashboard send, the HTTP response is all there is.
 * - `queued`          -- the slot was busy and the server queued the message.
 *                        The `queue_push` broadcast owns its on-screen card, so
 *                        this is NOT a receipt for an optimistic bubble, and a
 *                        queued message is still cancellable.
 * - `refused`         -- the server said no: a non-2xx status, or a readable
 *                        body with neither `ok` nor `queued`. Nothing was sent,
 *                        so the payload is safe to hand back. `reason` is the
 *                        server's own explanation when there is one.
 * - `unknown`         -- a 2xx whose body could not be read. The request WAS
 *                        accepted and only the answer is mangled, so the send
 *                        may well have started a turn; reporting it as a
 *                        failure would hand the payload back and invite a
 *                        retry that duplicates a delivered turn. Callers must
 *                        do NOTHING on this status.
 * - `response-late`   -- the abort deadline fired before a receipt arrived.
 *                        Delivery is indeterminate: a composer can keep its
 *                        optimistic row pending to avoid a duplicate, while a
 *                        caller that has already destroyed the only visible
 *                        copy may choose to recover it.
 * - `transport-error` -- the fetch itself rejected (offline, DNS, CORS). The
 *                        send never left, so restore-and-report is safe.
 */
export type SendReceiptStatus =
  | 'dispatched'
  | 'queued'
  | 'refused'
  | 'unknown'
  | 'response-late'
  | 'transport-error'

export interface SendReceipt {
  status: SendReceiptStatus
  /** The parsed acceptance body -- `{}` when no readable body exists. Passed
   *  through so card/ask logic that reads the raw acceptance
   *  (`resolveAskAfterSend`) keeps working unchanged. */
  body: SendReceiptBody
  /** Server-provided explanation, present only on `refused` with a readable
   *  body. */
  reason?: string
}

export interface SendTurnOptions {
  /** Wire text, already serialized (dir tokens, file markers). The server
   *  refuses an empty wire text above every dispatch branch, so an empty
   *  message comes back as `refused`. */
  message: string
  slot?: string
  meta?: Record<string, unknown>
}

/**
 * The one send implementation (chat-core transport layer).
 *
 * Owns the whole receipt contract so call sites stop re-learning it:
 * `POST /api/chat?ws=1` RESOLVES on HTTP failure (the fetch promise rejects
 * only on transport-level errors or abort), the body's verdict is read through
 * the shared `readSendReceipt` classifier, and a hung POST is bounded by
 * `SEND_ABORT_MS`. Callers branch on `receipt.status`; how to REACT (error
 * rows, composer restore, optimistic confirms, drafts) stays host policy at
 * the surface.
 *
 * Never rejects: every outcome, including transport failure, is a receipt.
 */
export async function sendTurn(opts: SendTurnOptions): Promise<SendReceipt> {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), SEND_ABORT_MS)
  try {
    const r = await api.sendChat(
      opts.message,
      opts.slot,
      undefined,
      controller.signal,
      opts.meta,
    )
    const { body, outcome } = await readSendReceipt(r)
    if (outcome === 'refused') return { status: 'refused', body, reason: typeof body.error === 'string' ? body.error : undefined }
    // readSendReceipt deliberately converts an unreadable accepted body into
    // `unknown`, including an AbortError raised while response.json() is still
    // consuming a stalled body. Preserve the deadline signal here: callers
    // recover input for `response-late`, while they correctly do nothing for a
    // merely malformed 2xx receipt (which may already have delivered the turn).
    if (outcome === 'unknown') {
      return { status: controller.signal.aborted ? 'response-late' : 'unknown', body }
    }
    // Only an IMMEDIATE dispatch is a delivery receipt for an optimistic
    // bubble: the busy branch sets BOTH flags, so `ok` alone proves nothing.
    if (confirmedDelivered(body)) return { status: 'dispatched', body }
    return { status: 'queued', body }
  } catch (e: unknown) {
    if (e instanceof DOMException && e.name === 'AbortError') return { status: 'response-late', body: {} }
    return { status: 'transport-error', body: {} }
  } finally {
    clearTimeout(timeout)
  }
}
