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

export interface SendChatReceipt {
  ok: boolean
  queued: boolean
  steered: boolean
  accepted: boolean
  refused: boolean
  readable: boolean
  error?: string
}

/** Parse the wire receipt once and keep an unreadable response distinct from an
 * explicit `{ok: false, queued: false}` refusal. */
export async function sendChatReceipt(response: Response): Promise<SendChatReceipt> {
  let value: unknown
  try {
    value = await response.json()
  } catch {
    value = undefined
  }

  const readable = typeof value === 'object' && value !== null && !Array.isArray(value)
  const body = readable ? value as Record<string, unknown> : {}
  const ok = body.ok === true
  const queued = body.queued === true
  const accepted = ok || queued

  return {
    ok,
    queued,
    steered: body.steered === true,
    accepted,
    refused: readable && !accepted,
    readable,
    ...(typeof body.error === 'string' ? { error: body.error } : {}),
  }
}
