import { api } from '../../api/client'
import type { SendRaceFields } from '../../api/client/chat'
import { noteUserChat } from '../../api/membersQuery'
import { queryClient } from '../../api/queryClient'
import { noteCrewSteerReceipt } from '../../lib/crewSteerNotice'
import { confirmedDelivered, readSendReceipt, SendReceiptBody, SendResponseLike } from '../../utils/sendDelivery'

/** The one send-id minter lives in `utils/sendDelivery`; re-exported here so a
 *  transport caller imports the minter and the send from one module. */
export { mintSendId } from '../../utils/sendDelivery'

/** Stop waiting on a send's response. Reaching this bound says only that no
 *  receipt arrived in time: usually the request was received and the reply is
 *  late (the turn is running and its output arrives over the WebSocket, not
 *  through this promise), but a POST that never reached the gateway looks the
 *  same from here. Delivery is INDETERMINATE -- not a failure signal and not a
 *  delivery receipt -- which is why the receipt below gives it its own status
 *  (`response-late`) instead of folding it into either the accepted or the
 *  error shapes. */
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
 *                        bubble. A correlated user echo can also confirm it;
 *                        the receipt must not append a second user row.
 * - `queued`          -- the slot was busy and the server queued the message.
 *                        The `queue_push` broadcast owns its on-screen card, so
 *                        this is NOT a receipt for an optimistic bubble, and a
 *                        queued message is still cancellable.
 * - `refused`         -- the send was refused: the server said no (a non-2xx
 *                        status, or a readable body with neither `ok` nor
 *                        `queued`), or the caller refused before sending.
 *                        Nothing was sent, so the payload is safe to hand back.
 *                        `reason` is the explanation when there is one.
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
 * - `transport-error` -- the fetch itself rejected without a response. Usually
 *                        the request never left (offline, DNS, CORS), but a
 *                        connection reset AFTER the server took the POST rejects
 *                        the same way, so delivery is INDETERMINATE -- the
 *                        request may have started a turn. What to do is
 *                        call-site policy: a composer restores the text and
 *                        reports (a visible duplicate beats a silent loss); a
 *                        caller whose retry would destroy or duplicate work
 *                        (a seeder deleting its slot) must not treat this as
 *                        proof that nothing ran.
 */
export type SendReceiptStatus =
  | 'dispatched'
  | 'queued'
  | 'refused'
  | 'unknown'
  | 'response-late'
  | 'transport-error'

/**
 * `Error.name` a caller that must REJECT on a receipt stamps on the error it
 * throws for a `refused` receipt, so the surface that catches it can tell a
 * definite refusal (the message is the server's own reason, fit to show
 * verbatim) from an unconfirmed dispatch. One spelling here rather than one
 * per app.
 */
export const SEND_REFUSED = 'send-refused'

export interface SendReceipt {
  status: SendReceiptStatus
  /** The parsed acceptance body -- `{}` when no readable body exists. Passed
   *  through so card/ask logic that reads the raw acceptance
   *  (`resolveAskAfterSend`) keeps working unchanged. */
  body: SendReceiptBody
  /** Why a `refused` send was refused: the server's explanation when its body
   *  is readable, or the caller's own when it refuses before sending (Side Chat
   *  holding a question until its restore read lands). */
  reason?: string
}

/** What a send puts on the wire. Surface-neutral: the endpoint, the auth
 *  header and any surface-specific body fields (an app embed's `agent`) are
 *  the wire's business, not the caller's. */
export interface SendWirePayload {
  message: string
  slot?: string
  meta?: Record<string, unknown>
  /** See `SendTurnOptions.steer`. Only the dashboard wire forwards it: an
   *  app embed cannot steer (the server refuses app-authenticated steers). */
  steer?: boolean | 'auto'
  /** Human crewmate DM priority, validated by the gateway. Dashboard only. */
  responsive?: boolean
  /** See `SendTurnOptions.colorTheme`. Dashboard wire only. */
  colorTheme?: string
  /** See `SendTurnOptions.race`. Dashboard wire only. */
  race?: SendRaceFields
}

/**
 * The fetch seam under `sendTurn`. A wire performs ONE `POST` and hands back
 * something the shared `readSendReceipt` classifier can read: it resolves
 * with the response on every HTTP status (a 4xx/5xx is a readable refusal,
 * never a rejection) and rejects only when the request itself failed to go
 * out, or with an `AbortError` once `signal` fires.
 *
 * An `AbortError` rejection is therefore how a wire says "INDETERMINATE": the
 * transport classifies it as `response-late` (the one outcome a caller may
 * recover by handing the text back, never by retrying). A wire whose
 * underlying request cannot tell "never left" from "accepted, then the
 * connection dropped before headers" -- a client helper's raw rejection, a
 * multi-call sequence that already dispatched -- MUST reject with
 * `indeterminate()` rather than pass the raw error through: the raw error
 * classifies as `transport-error`, which invites a retry-safe resend that runs
 * the turn twice if the POST did land. Only a wire that can PROVE the request
 * never went out may reject with anything else -- with one named exemption:
 * `dashboardSendWire` passes a raw fetch rejection through (→ `transport-error`)
 * because its surfaces (ChatPane, ChatPage) keep an optimistic bubble the user
 * can see and a resend path that checks the transcript first, so a retry
 * there is a visible, user-driven act, not a silent duplicate. An embed has
 * neither, which is why its wire must say indeterminate.
 *
 * The transport is one implementation; the wire is per surface. The
 * dashboard's own client is the default. An app-sdk embed reaches the same
 * endpoint through its permission-scoped `AppApi`, whose JSON helper throws
 * on non-2xx -- its wire adapter re-expresses that as a resolved refusal so
 * the classification above is the same for every caller.
 */
export type SendWire = (payload: SendWirePayload, signal: AbortSignal) => Promise<SendResponseLike>

/**
 * The one spelling of a wire's "indeterminate" rejection (see `SendWire`): the
 * `AbortError` the transport reads as `response-late`. Every wire that cannot
 * separate never-left from accepted-then-dropped rejects with this, so the
 * idiom is named once instead of each adapter minting its own `DOMException`.
 */
export function indeterminate(): DOMException {
  return new DOMException('aborted', 'AbortError')
}

/**
 * Run a wire's underlying request under the transport's deadline signal when
 * the request itself cannot take a signal (a client helper, a two-call
 * sequence). Resolves/rejects with `start()`'s outcome unless `signal` fires
 * first, in which case it rejects with `onAbort()` and ignores the late
 * settlement. One spelling of the abort race for every wire that needs it --
 * the race is easy to get subtly wrong (a late settlement flipping an already
 * delivered receipt), so it lives here rather than in each adapter.
 */
export function settleUnderSignal<T>(
  signal: AbortSignal,
  start: () => Promise<T>,
  onAbort: () => unknown = indeterminate,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    if (signal.aborted) { reject(onAbort()); return }
    let settled = false
    const abort = () => { if (!settled) { settled = true; reject(onAbort()) } }
    signal.addEventListener('abort', abort, { once: true })
    const done = <V,>(fn: (v: V) => void) => (v: V) => {
      if (settled) return
      settled = true
      signal.removeEventListener('abort', abort)
      fn(v)
    }
    start().then(done(resolve), done(reject))
  })
}

/** The dashboard client's wire: `POST /api/chat?ws=1` with the session key. */
const dashboardSendWire: SendWire = (payload, signal) =>
  payload.race
    ? api.sendChat(payload.message, payload.slot, payload.colorTheme, signal, payload.meta, payload.steer, payload.responsive ? true : undefined, payload.race)
    : api.sendChat(payload.message, payload.slot, payload.colorTheme, signal, payload.meta, payload.steer, ...(payload.responsive ? [true] : []))

export interface SendTurnOptions {
  /** Wire text, already serialized (dir tokens, file markers). The server
   *  refuses an empty wire text above every dispatch branch, so an empty
   *  message comes back as `refused`. */
  message: string
  slot?: string
  meta?: Record<string, unknown>
  /** "Act on this now": inject into the running turn instead of queueing
   *  behind it (or, on an idle slot, skip the hold that parks a message behind
   *  still-running sub-agents). A flag of THIS endpoint -- `/api/chat` reads
   *  it -- not a new receipt shape: a steer comes back `dispatched`, `queued`
   *  (demoted) or refused like any send, with `body.steered` saying which.
   *
   *  `'auto'` carries the same intent with the steer-or-queue choice handed to
   *  the gateway for this message (`decisions/points/message_steer.py`). Still not
   *  a new receipt shape: the answer comes back as the `dispatched` of a steer or
   *  the `queued` of a queue, which the two existing statuses already describe. */
  steer?: boolean | 'auto'
  /** Ask the gateway to prioritize a human crewmate DM without a client stop. */
  responsive?: boolean
  /** The active colour theme, so a widget the turn renders inherits it. Sent
   *  only by surfaces that own a theme (ChatPage); embeds and panes omit it. */
  colorTheme?: string
  /** Shared Crew first submit wins: the session revision this send is based on,
   *  a deliberate Send now after a lost race, and the retry key. A lost race
   *  comes back `refused` with a `send_race` body naming who sent first. */
  race?: SendRaceFields
  /** Which fetch seam carries the POST. Defaults to the dashboard client. */
  wire?: SendWire
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
  const wire = opts.wire ?? dashboardSendWire
  try {
    const r = await wire(
      {
        message: opts.message,
        slot: opts.slot,
        meta: opts.meta,
        steer: opts.steer,
        ...(opts.responsive ? { responsive: true } : {}),
        colorTheme: opts.colorTheme,
        ...(opts.race ? { race: opts.race } : {}),
      },
      controller.signal,
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
    // Accepted either way: the send is the DM's newest message, which the
    // Crewmates list orders by.
    noteUserChat(queryClient, opts.slot)
    // A person's steer into a Crew Mode worker says whether it reached the
    // worker's ledger; a delivered-but-unrecorded one is shown under its composer.
    if (opts.slot) noteCrewSteerReceipt(opts.slot, body)
    if (confirmedDelivered(body)) return { status: 'dispatched', body }
    return { status: 'queued', body }
  } catch (e: unknown) {
    if (e instanceof DOMException && e.name === 'AbortError') return { status: 'response-late', body: {} }
    return { status: 'transport-error', body: {} }
  } finally {
    clearTimeout(timeout)
  }
}
