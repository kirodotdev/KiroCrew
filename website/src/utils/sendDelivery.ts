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
 *                 status; or an intermediary answered in the endpoint's place, so
 *                 the request never reached it. Nothing was sent, so the payload
 *                 is safe to hand back.
 *  - `unknown`  — the request was accepted (2xx) by the endpoint itself but its
 *                 body could not be read. The message may well have been
 *                 delivered, so the payload must NOT be handed back.
 */
export type SendOutcome = 'accepted' | 'refused' | 'unknown'

export interface SendReceipt {
  /** The parsed body, or `{}` when the response carried no readable JSON object. */
  body: SendReceiptBody
  outcome: SendOutcome
}

/** The part of `Response` a receipt is read from — narrowed so a test can stand
 *  one up without constructing a whole `Response`.
 *
 *  The two provenance members are OPTIONAL so every existing double stays valid;
 *  a double that omits them reads as "not redirected, no content type", which is
 *  the pre-existing behaviour. */
export interface SendResponseLike {
  ok: boolean
  json(): Promise<unknown>
  /** Whether the browser followed a redirect chain to produce this response. */
  redirected?: boolean
  headers?: { get(name: string): string | null }
}

/**
 * Positive evidence that a 2xx response was written by an INTERMEDIARY rather
 * than by the send endpoint — the shape an SSO/auth proxy produces once the
 * browser's session with it has lapsed.
 *
 * Both signals are about PROVENANCE, not about content:
 *
 *   - `redirected` — the browser followed a redirect chain, so whatever answered
 *     sits at the end of that chain. `POST /api/chat` never redirects, so a
 *     redirected answer did not come from it.
 *   - an HTML content type — the endpoint answers JSON on every path, refusals
 *     included, so `text/html` is a page (a login form), not a receipt.
 *
 * Deliberately NOT "the body would not parse". That is `unknown`'s case and it
 * stays exactly as it was: a TRUNCATED gateway reply to a POST that did run must
 * keep its silence, because handing the payload back there duplicates a
 * delivered turn (the regression #5672 fixed). Only a response the gateway
 * demonstrably did not write is reclassified.
 */
function answeredByIntermediary(response: SendResponseLike): boolean {
  if (response.redirected) return true
  const contentType = response.headers?.get('content-type') ?? ''
  return /^\s*text\/html\b/i.test(contentType)
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
    // An intermediary answered in the endpoint's place, so the POST never
    // reached it. That is a refusal in the one sense every call site acts on —
    // "nothing was sent, so the payload is safe to hand back" — and it is the
    // difference between the composer keeping the user's text and dropping it.
    //
    // Without this, an auth proxy's login page (a 2xx that will not parse) took
    // `unknown`'s silent branch: no error row, no banner, and a composer already
    // cleared at submit, so the message was lost with nothing on screen saying
    // so. `unknown` still owns the case it was written for, one line below.
    if (answeredByIntermediary(response)) return { body, outcome: 'refused' }
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
 *  as a conservative default -- it is the wrong question. Two properties of the
 *  busy branch make a queued response unusable as a receipt for THIS bubble:
 *
 *  - It queues only a NON-EMPTY message (`if message: slot.queue_append(...)`)
 *    yet answers `{ok: true, queued: true}` either way, so a file-only send that
 *    races into it is dropped behind a success-shaped body.
 *  - When it does queue, it broadcasts `queue_push`, and that card is the
 *    server-owned representation of the message. The optimistic bubble is then a
 *    duplicate whose fate is not the row's: cancelling the queued message removes
 *    the card and leaves the bubble behind.
 *
 *  In both shapes "confirmed" would be a lie about a message that never ran, so
 *  a queued acceptance leaves the bubble pending and the 30s indicator keeps its
 *  say. This costs nothing in the ordinary case: a send made while the slot is
 *  visibly busy appends no optimistic bubble at all, so there is nothing to
 *  confirm -- only the client-thought-idle race reaches here, and that is exactly
 *  the case worth warning about.
 */
export function confirmedDelivered(body: { ok?: boolean; queued?: boolean }): boolean {
  return !!body.ok && !body.queued
}
