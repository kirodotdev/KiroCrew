/** Shape of `POST /api/chat`'s JSON receipt, as far as the send paths read it. */
export interface SendReceiptBody {
  /** The server dispatched the message immediately. */
  ok?: boolean
  /** The slot was busy, so the message was parked on its queue instead. */
  queued?: boolean
  /** A steer-flagged send was injected into the running turn. */
  steered?: boolean
  /** The server-minted stable id (`meta.mid`) of the user row this send
   *  appended. Present only on a confirmed immediate dispatch; the client
   *  stamps it onto the optimistic bubble so message-pinning works before the
   *  chat_done refresh rebuilds the transcript from disk. */
  mid?: string
  /** Server-authored refusal prose. Typed `unknown` because it arrives off the
   *  wire: a caller that renders it must narrow it to a string first. */
  error?: unknown
  [key: string]: unknown
}

/** What the response proves about the message the composer just cleared.
 *
 *  - `accepted` — the server said `ok` or `queued`; the message is its problem now.
 *  - `refused`  — the server said no, either in a readable body or with a non-2xx
 *                 status. Nothing was sent, so the payload is safe to hand back.
 *  - `unknown`  — the request was accepted (2xx) but its body could not be read.
 *                 The message may well have been delivered.
 */
export type SendOutcome = 'accepted' | 'refused' | 'unknown'

export interface SendReceipt {
  /** The parsed body, or `{}` when the response carried no readable JSON object. */
  body: SendReceiptBody
  outcome: SendOutcome
}

/** The part of `Response` a receipt is read from — narrowed so a test can stand
 *  one up without constructing a whole `Response`. */
export interface SendResponseLike {
  ok: boolean
  json(): Promise<unknown>
}

/**
 * Classify the send endpoint's answer, keeping "refused" and "unreadable" apart.
 *
 * Every send path used to fold an unreadable body into `{}` and then test it for
 * the acceptance flags, so a TRUNCATED reply to an accepted POST answered the
 * same as an explicit refusal: the user was told the send failed and handed the
 * payload back to retry, which duplicates a turn that did go out — side effects
 * included. The status line is the one piece of a mangled response that survives,
 * so it decides that case:
 *
 *   - a NON-2XX status is a refusal in its own right, body or no body (an aiohttp
 *     500 answers in HTML, and a proxy's 502 page is not JSON either), which is
 *     what these paths have always reported for it;
 *   - a 2XX whose body will not parse proves only that the request was accepted.
 *     That is `unknown`, and an unknown must never be reported as a refusal.
 *
 * A JSON body that is not an object (`null`, an array, a bare string) is treated
 * as unreadable rather than as a receipt with absent flags — reading acceptance
 * flags off an array would call a 200 a refusal for the same wrong reason.
 */
export async function readSendReceipt(response: SendResponseLike): Promise<SendReceipt> {
  let parsed: unknown
  try {
    parsed = await response.json()
  } catch {
    parsed = undefined
  }
  const readable = !!parsed && typeof parsed === 'object' && !Array.isArray(parsed)
  const body = readable ? (parsed as SendReceiptBody) : {}
  if (!response.ok) return { body, outcome: 'refused' }
  if (!readable) {
    // The one outcome with NO user-facing trace, by design — so it needs a
    // diagnostic one, or an intermediary that mangles every receipt degrades
    // sends invisibly and leaves nobody anything to find. Console only: this
    // is a developer signal, not copy, so it earns no catalog key.
    // eslint-disable-next-line no-console -- the silent branch's only trail
    console.warn('send receipt unreadable on an accepted response — delivery unknown')
    return { body, outcome: 'unknown' }
  }
  return { body, outcome: body.ok || body.queued ? 'accepted' : 'refused' }
}

/** Did `POST /api/chat` actually take custody of this message, as the row the
 *  optimistic bubble stands for?
 *
 *  Only an IMMEDIATE dispatch counts. `queued` is deliberately excluded, and not
 *  as a conservative default -- it is the wrong question. The busy branch
 *  broadcasts `queue_push`, and that card is the server-owned representation of
 *  the message. The optimistic bubble is then a duplicate whose fate is not the
 *  row's: cancelling the queued message removes the card and leaves the bubble
 *  behind.
 *
 *  So "confirmed" would be a lie about a message that never ran, so
 *  a queued acceptance leaves the bubble pending and the 30s indicator keeps its
 *  say. This costs nothing in the ordinary case: a send made while the slot is
 *  visibly busy appends no optimistic bubble at all, so there is nothing to
 *  confirm -- only the client-thought-idle race reaches here, and that is exactly
 *  the case worth warning about.
 */
export function confirmedDelivered(body: { ok?: boolean; queued?: boolean }): boolean {
  return !!body.ok && !body.queued
}

/** What a user row's delivery markers add up to, as ONE value.
 *
 *  `none` renders plainly; `unknown` is live doubt (this send may still duplicate
 *  a turn if resent); `spent` is doubt a later send's confirmation demoted, kept
 *  visible in past tense rather than erased.
 *
 *  The point of deriving it here is that the PRECEDENCE lives in one place. The
 *  markers are independent booleans, so `deliveryConfirmed` alongside a stale
 *  `deliveryUnknown`/`deliveryUnresolved` is representable — nothing clears the
 *  demotion flags when a send confirms. Every reader resolving that on its own is
 *  how a bubble ends up muted while the composer beside it reads "delivered".
 *  Confirmation wins, then live doubt, then spent doubt.
 */
export type RowDeliveryState = 'none' | 'unknown' | 'spent'

export function rowDeliveryState(meta: Record<string, unknown> | undefined): RowDeliveryState {
  if (meta?.deliveryConfirmed === true) return 'none'
  if (meta?.deliveryUnknown === true) return 'unknown'
  if (meta?.deliveryUnresolved === true) return 'spent'
  return 'none'
}

/** Does this row carry positive proof that `sendId` landed?
 *
 *  Separate from `rowDeliveryState`, which collapses confirmed and never-claimed to
 *  the same `none` because a renderer treats them identically — that answer cannot
 *  serve a caller asking about ONE send. The two spellings are the reason this is an
 *  accessor rather than a field read: the confirming row names the send either as
 *  `confirmedSendId`, as its own `sendId`, or — once a drain merges rows — inside `sendIds`,
 *  where the scalar keeps only the last writer and a caller checking it under-matches.
 *
 *  Proof is EITHER the client's own `deliveryConfirmed` or a server `mid`, because the former
 *  does not survive a reload while the fetched row that carries the latter is itself the proof.
 */
export function confirmsSend(meta: Record<string, unknown> | undefined, sendId: string): boolean {
  if (!sendId) return false
  const names = meta?.confirmedSendId === sendId
    || meta?.sendId === sendId
    || (Array.isArray(meta?.sendIds) && (meta.sendIds as unknown[]).includes(sendId))
  if (!names) return false
  // A SERVER-fetched row is proof on its own: `deliveryConfirmed` is client-only, so requiring
  // it made a reload revert an already-definite caption to the resend hedge.
  if (typeof meta?.mid === 'string' && meta.mid) return true
  return meta?.deliveryConfirmed === true
}
