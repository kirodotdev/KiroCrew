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
