/**
 * The chat-core transport's wire for the side panel (`/side/open` + `/side/turn`).
 *
 * The side panel is the one chat surface on a DIFFERENT endpoint family, with
 * the OPPOSITE receipt semantics from `POST /api/chat?ws=1`: the dashboard
 * client's `api.sideOpen` / `api.sideTurn` go through `j()`, which RESOLVES the
 * parsed JSON on 2xx and REJECTS with an `ApiError` (friendly message, status,
 * raw body; auth-expiry and error recording already handled) on non-2xx. The
 * shared `readSendReceipt` classifier wants the fetch seam's shape instead --
 * a response that resolves on every HTTP status. This adapter re-expresses the
 * side calls in that shape so the side panel's receipt is classified by the
 * same rule as every other surface's:
 *
 * - open ok + turn 2xx JSON     -> resolved `{ ok: true }` whose `json()` yields
 *                                 the body. `{ok}` -> `dispatched`; `{ok, queued}`
 *                                 -> `queued`; both pass the side-specific fields
 *                                 (`run_id`, `queue_id`, `steer_id`, `pending`,
 *                                 `demoted`, `still_queued`) through untouched.
 * - `ApiError` from either call -> resolved `{ ok: false }` whose body carries
 *                                 the error's friendly message, i.e. `refused`
 *                                 with the reason the panel already displayed.
 *                                 `j()` has run, so auth handling and error
 *                                 recording happened exactly as before.
 * - `/side/turn` 2xx, body unreadable
 *                               -> resolved `{ ok: true }` whose `json()` rejects,
 *                                 i.e. `unknown`. `api.sideTurn` tags a
 *                                 post-2xx body-read failure (stream cut OR
 *                                 unparseable) as `AcceptedBodyUnreadable`; the
 *                                 server ACCEPTED the turn, so it must not be
 *                                 classified as a failure that hands the text
 *                                 back for a duplicate retry.
 * - deadline DURING `/side/turn` -> rejected `AbortError`, i.e. `response-late`.
 *                                 The client helpers take no signal, so the
 *                                 in-flight request is not cancelled; the
 *                                 deadline is honoured at the receipt.
 *                                 `/side/turn` is an acceptance receipt (the
 *                                 turn runs in the background), so a late
 *                                 answer means a stalled request, not a slow
 *                                 turn.
 * - deadline BEFORE `/side/turn` -> rejected with a plain error, i.e.
 *   (a stalled `/side/open`)      `transport-error`: nothing was accepted, so
 *                                 the caller rolls back and restores. The
 *                                 sequence also stops -- the turn is never
 *                                 dispatched after the receipt was delivered.
 *                                 (Classifying this as `response-late` would
 *                                 strand an idle send's optimistic bubble for a
 *                                 turn that will never run.)
 * - anything else               -> rejected, i.e. `transport-error`: the request
 *                                 never left (offline, DNS).
 *
 * `/side/open` is idempotent and cheap; it stays in the wire so the two-call
 * sequence the panel has always made is one send from the caller's point of
 * view.
 */
import { api } from '../../api/client'
import { ApiError, AcceptedBodyUnreadable } from '../../api/apiError'
import { settleUnderSignal, type SendWire } from '../../chat-core/transport/sendTurn'
import type { SendResponseLike } from '../../utils/sendDelivery'

function refusal(err: ApiError): SendResponseLike {
  const error = err.message
  return { ok: false, json: () => Promise.resolve({ error }) }
}

/** The deadline fired before `/side/turn` was ever sent (a stalled
 *  `/side/open`). Nothing was accepted, so this is a transport failure the
 *  caller may restore from -- NOT the indeterminate `response-late`, which
 *  would leave an idle send's optimistic bubble stranded for a turn that will
 *  never run. A plain `Error`: `sendTurn` distinguishes only `AbortError`, and
 *  no message because the panel renders its own connection copy. */
const preTurnTimeout = () => new Error()

/** `steer` injects into the running turn instead of queueing. It rides the
 *  wire, not the transport contract: only this endpoint family understands it. */
export function sideTurnWire(slot: string, steer?: boolean): SendWire {
  return (payload, signal) => {
    let turnStarted = false
    return settleUnderSignal<SendResponseLike>(
      signal,
      () => api.sideOpen(slot)
        .then(() => {
          // The deadline may have fired while `/side/open` was in flight; the
          // receipt is already on its way. Dispatching the turn now would send
          // text the user is being handed back.
          if (signal.aborted) return undefined as never
          turnStarted = true
          return api.sideTurn(slot, payload.message, steer ? { steer: true } : undefined)
        })
        .then(
          (body: unknown): SendResponseLike => ({ ok: true, json: () => Promise.resolve(body) }),
          (err: unknown): SendResponseLike => {
            if (err instanceof ApiError) return refusal(err)
            // `/side/turn` answered 2xx and only the body was lost (stream cut,
            // unparseable). The turn is running; `unknown`, never a retry.
            if (err instanceof AcceptedBodyUnreadable) return { ok: true, json: () => Promise.reject(err.reason) }
            throw err
          },
        ),
      // Abort BEFORE the turn request was sent = nothing accepted = a plain
      // failure (`transport-error`); abort DURING it = indeterminate
      // (`response-late`).
      () => (turnStarted ? new DOMException('aborted', 'AbortError') : preTurnTimeout()),
    )
  }
}
