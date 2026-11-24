/** Reading a slot's waiting-on-you asks for every dashboard surface.
 *
 *  An ask is the agent's standing request to the person ("post the override",
 *  "pick a branch"). It is labelled "Action for you" everywhere, never "Waiting
 *  on you": that phrase already means a live question card or tool approval,
 *  and a standing ask must not be mistaken for one. A live question or approval
 *  therefore outranks an ask on every status surface (`actionForYou`). */
import type { ChatSlot, WaitingOnYouAsk } from '../types'

const NO_ASKS: readonly WaitingOnYouAsk[] = Object.freeze([])

/** The slot's pinned asks in stack order, oldest declaration first. */
export function pinnedAsks(slot: Pick<ChatSlot, 'waiting_on_you'> | null | undefined): readonly WaitingOnYouAsk[] {
  const asks = slot?.waiting_on_you
  return Array.isArray(asks) && asks.length > 0 ? asks : NO_ASKS
}

/** The newest declaration: the last in stack order. A replaced ask keeps its
 *  place, so the card shown in full does not jump when the agent rewords one. */
export function newestAsk(slot: Pick<ChatSlot, 'waiting_on_you'> | null | undefined): WaitingOnYouAsk | null {
  const asks = pinnedAsks(slot)
  return asks.length > 0 ? asks[asks.length - 1] : null
}

/** The ask a status surface shows, or null when a live approval or question
 *  card owes the person something more urgent, or nothing is pinned. */
export function actionForYou(
  slot: Pick<ChatSlot, 'waiting_on_you' | 'pending_approval' | 'needs_input'> | null | undefined,
): WaitingOnYouAsk | null {
  if (!slot || slot.pending_approval || slot.needs_input) return null
  return newestAsk(slot)
}
