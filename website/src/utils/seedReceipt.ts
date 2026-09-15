/**
 * Receipt policy for a NON-INTERACTIVE SEED -- a first message an app sends into
 * a slot it just created, with no composer to restore into (issue-radar and
 * auto-improvement `agentSession.openSession`). The statuses' MEANINGS live on
 * `SendReceiptStatus` in chat-core; this module is only what a seeder does with
 * them, kept in one place so the two seeders cannot drift apart:
 *
 * - `refused`: the server said no, so nothing is running -- the seed never ran.
 * - `transport-error` / `response-late`: no receipt at all (the fetch rejected,
 *   or the deadline passed first). Delivery is indeterminate -- a connection
 *   reset AFTER the server took the POST rejects exactly like a request that
 *   never left, and a stalled POST may still land -- so instead of guessing the
 *   seeder ASKS the slot. The seed is stored as the slot's first message before
 *   its turn runs, so a one-row slot read settles it: a message means the seed
 *   is (or may be) running; an empty slot, re-read a bounded number of times to
 *   let a late arrival land, means it did not. A probe the server cannot
 *   answer reads as "may be running" -- deleting on a guess would cancel real
 *   work, and leaving a live slot unrecorded would orphan it for the next click
 *   to duplicate. A slot the server reports GONE is not running the seed.
 *
 *   Accepted residual: the verdict is only as good as the probe window. A POST
 *   that reaches the gateway AFTER the last re-read (held by a proxy or buffer
 *   past the client-side reset or abort) finds the slot deleted, and
 *   `/api/chat` re-creates a missing slot, so that seed runs in an unrecorded
 *   session while the user was told it did not start. For a rejected fetch this
 *   is the old always-orphan outcome confined to a corner; for the deadline it
 *   is a NEW corner, bought deliberately: the old seeder had no deadline, so a
 *   stalled POST hung the launch until it resolved and was then recorded. The
 *   trade is a bounded wait plus a small late-landing window against an
 *   unbounded hang. The window (`SEED_PROBE_ATTEMPTS` x `SEED_PROBE_GAP_MS`) is
 *   a judgement, not a measurement; widen it if late landings turn up. Closing
 *   the corner outright needs the send to opt out of slot auto-create
 *   server-side, which is not this caller's to add.
 * - `unknown`: a 2xx whose body could not be read -- accepted; the seed ran.
 * - `dispatched` / `queued`: the seed ran.
 */
import { api } from '../api/client'
import type { SendReceipt } from '../chat-core/transport/sendTurn'
import { i18nT } from '../i18n/t'
import { isMissingSlotError } from './thunkError'

/** How many times an empty slot is re-read before a no-receipt send is ruled
 *  never-landed, and the gap between reads. A judgement call sizing the
 *  late-landing window described above, not a measured figure. */
export const SEED_PROBE_ATTEMPTS = 3
export const SEED_PROBE_GAP_MS = 1000

export type SeedVerdict =
  | { ran: true }
  /** The seed provably never ran; `reason` is the user-facing text. */
  | { ran: false; reason: string }

/** The user-facing text of a seed that did not start: the core's own "could not
 *  start" copy, with the server's reason appended when it gave one. Never a raw
 *  status enum -- this lands in the launch button's error notice. */
function seedFailureText(reason?: string): string {
  const base = i18nT('pages.chatPage.could_not_start_a_new_session') as string
  return reason ? `${base} (${reason})` : base
}

/** What the slot says about the seed: it is there, the slot is still empty, the
 *  slot is gone, or the server could not answer. */
async function probeSlot(slotKey: string): Promise<'landed' | 'empty' | 'gone' | 'unknown'> {
  try {
    const detail = (await api.chatSlotDetail(slotKey, 1)) as { total?: unknown; messages?: unknown }
    const count = typeof detail.total === 'number'
      ? detail.total
      : Array.isArray(detail.messages) ? detail.messages.length : 0
    return count > 0 ? 'landed' : 'empty'
  } catch (e) {
    return isMissingSlotError(e) ? 'gone' : 'unknown'
  }
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

/**
 * Settle a seed's receipt into "ran" or "never ran". Only `never ran` may tear
 * the slot down; see the module comment for why each status lands where it does.
 */
export async function settleSeedReceipt(receipt: SendReceipt, slotKey: string): Promise<SeedVerdict> {
  if (receipt.status === 'refused') return { ran: false, reason: seedFailureText(receipt.reason) }
  if (receipt.status !== 'transport-error' && receipt.status !== 'response-late') return { ran: true }
  for (let attempt = 1; ; attempt++) {
    const probe = await probeSlot(slotKey)
    if (probe === 'landed' || probe === 'unknown') return { ran: true }
    if (probe === 'gone' || attempt >= SEED_PROBE_ATTEMPTS) return { ran: false, reason: seedFailureText() }
    await sleep(SEED_PROBE_GAP_MS)
  }
}
