/** Reading a slot's waiting-on-you asks, and the refusals their routes answer.
 *
 *  An ask is the agent's standing request to the person ("post the override",
 *  "pick a branch"). It is labelled "Action for you" everywhere, never "Waiting
 *  on you": that phrase already means a live question card or tool approval,
 *  and a standing ask must not be mistaken for one. A live question or approval
 *  therefore outranks an ask on every status surface (`actionForYou`). */
import { ApiError } from '../api/apiError'
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

/** A refusal the answer and dismiss routes name with a `code`. `superseded`
 *  and `cleared` mean the agent replaced or withdrew the ask, `answered` that
 *  another window or surface answered it first, `uncertain` that an earlier
 *  answer may already have been sent. */
export type WaitingStaleCode = 'answered' | 'superseded' | 'cleared'
export type WaitingRefusal =
  | { kind: 'stale'; code: WaitingStaleCode }
  | { kind: 'uncertain' }
  | { kind: 'invalid' }
  /** Any other refusal of the underlying send (a paused crew, a full queue):
   *  the ask stays pinned. `reason` is the gateway's own sentence, if any. */
  | { kind: 'refused'; code: string; reason: string }
  /** No answer from the gateway, or one that names no code: retryable. */
  | { kind: 'failed' }

function bodyOf(err: ApiError): { code?: unknown; error?: unknown } {
  try {
    const parsed: unknown = JSON.parse(err.body)
    return parsed && typeof parsed === 'object' ? parsed as { code?: unknown; error?: unknown } : {}
  } catch {
    return {}
  }
}

const STALE_CODES: ReadonlySet<string> = new Set(['answered', 'superseded', 'cleared'])

export function waitingRefusal(err: unknown): WaitingRefusal {
  if (!(err instanceof ApiError)) return { kind: 'failed' }
  const body = bodyOf(err)
  const code = typeof body.code === 'string' ? body.code : ''
  if (err.status === 409 && STALE_CODES.has(code)) return { kind: 'stale', code: code as WaitingStaleCode }
  if (err.status === 409 && code === 'uncertain') return { kind: 'uncertain' }
  if (err.status === 400 && code === 'invalid_answer') return { kind: 'invalid' }
  if (code) return { kind: 'refused', code, reason: typeof body.error === 'string' ? body.error : '' }
  return { kind: 'failed' }
}
