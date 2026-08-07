/**
 * Delivery state for a sealed request — resolved by ASKING the session, not by
 * remembering.
 *
 * `POST /send` seals a batch server-side; the panel builds and dispatches the
 * prompt afterwards, then acknowledges. Those are three steps over two
 * processes, so a crash or a lost ack leaves a sealed request whose fate the
 * panel cannot infer from its own memory — and guessing wrong in either
 * direction is bad: guess "not sent" and a resend applies every edit twice,
 * guess "sent" and the batch is stranded.
 *
 * So don't guess. The prompt carries the request id (in the payload path and in
 * each `[cid …]`), and `POST /api/chat/slots` returns the slot's recent
 * transcript plus its pending queue. Searching those for the id is ground truth:
 * either the session has this batch or it does not.
 *
 * Pure functions, because the cost of getting this branch wrong is duplicated
 * edits in someone's repository.
 */

import type { Request, SlotTranscript } from './types'

/** Does this request still need something done to complete delivery? */
export function needsDeliveryRetry(req: Pick<Request, 'deliveredAt'>): boolean {
  return !req.deliveredAt
}

/**
 * Is this request's batch present in the session — delivered or merely queued?
 *
 * The queue half is load-bearing: seconds after a send the prompt is accepted
 * but not yet turned into a message, so a transcript-only check would read
 * "absent" and offer a resend that duplicates it.
 *
 * A miss is only decisive within the window the host returns (the recent
 * transcript, not all history). `deliveryVerdict` is what encodes that limit;
 * this function answers the narrower question.
 */
export function transcriptHasRequest(t: SlotTranscript | null, requestId: string): boolean {
  if (!t || !requestId) return false
  const hit = (s: unknown) => typeof s === 'string' && s.includes(requestId)
  const messages = Array.isArray(t.messages) ? t.messages : []
  const queue = Array.isArray(t.queue) ? t.queue : []
  return (
    messages.some((m) => hit((m as { content?: unknown } | null)?.content)) ||
    queue.some((q) => hit((q as { content?: unknown } | null)?.content))
  )
}

/**
 * `delivered` — the session has it; mark it and drop the retry affordance.
 * `missing`   — the session does not have it; offering a send is safe.
 * `unknown`   — no transcript to judge by (the lookup failed), so claim nothing.
 *
 * `unknown` exists so a failed lookup cannot masquerade as either answer: it
 * leaves the request exactly as it was rather than silently marking it delivered
 * or inviting a duplicate.
 */
export type DeliveryVerdict = 'delivered' | 'missing' | 'unknown'

export function deliveryVerdict(
  req: Pick<Request, 'id' | 'deliveredAt'>,
  t: SlotTranscript | null,
): DeliveryVerdict {
  if (req.deliveredAt) return 'delivered'
  if (!t) return 'unknown'
  return transcriptHasRequest(t, req.id) ? 'delivered' : 'missing'
}
