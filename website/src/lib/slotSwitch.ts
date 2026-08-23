/** Latest-request-wins bookkeeping for optimistic slot-field switches (#4523).
 *
 *  A switch handler fires an API call and, on success, writes the result into
 *  the Redux slot row so the acting surface sees its own pick without waiting
 *  on the server's coalesced `slots` rebroadcast (which never arrives at all
 *  when the websocket is down). Two switches can be in flight together — the
 *  model dropdown deliberately stays open after a pick, and the Alt+Shift
 *  cycle shortcuts fire once per keypress — so writes must be adjudicated, or
 *  an out-of-order response would relabel the chip with a superseded pick.
 *
 *  One module-level registry (keyed `field:slot`) rather than per-component
 *  refs, so every control that writes the same field of the same slot shares
 *  one sequence: a dropdown pick racing a cycle press cannot interleave
 *  stale, and a press on one slot never suppresses another slot's write.
 *
 *  `pending` carries the newest in-flight TARGET so burst-stepping consumers
 *  (the cycle shortcuts) can advance from what the previous press already
 *  requested instead of recomputing from a store base that has not settled
 *  yet — without it a rapid triple-press computes the same "next" three times
 *  and lands one step ahead instead of three.
 *
 *  THE ADJUDICATION MODEL. `performSlotSwitch` dispatches requests for one
 *  slot+field strictly one at a time, in ticket order — two rapid picks on
 *  separate pooled connections could otherwise arrive at the gateway
 *  newest-first, leaving the backend on the OLDER pick while the registry
 *  declared the newer one authoritative. With dispatch serialized, send order
 *  IS server processing order, and since a failed request changes nothing
 *  server-side, the value actually in force is the HIGHEST-SEQ REQUEST THAT
 *  SUCCEEDED.
 *
 *  The chain is NEVER advanced past an unsettled request. Without an abort
 *  signal on the api client (a follow-up), a request that has merely stalled
 *  may still be alive in transit, and dispatching its successor early would
 *  let the two arrive at the gateway in either order — the stalled OLDER
 *  pick could land after the newer one and become what the backend runs,
 *  invisibly. What IS bounded is the CALLER's wait: after
 *  SWITCH_CONFIRM_TIMEOUT_MS the pick is reported unconfirmed (the failure
 *  toast fires) while its wire call keeps its place in the chain. A dead
 *  connection therefore never silently freezes the picker — every pick
 *  answers within the budget — and no amount of stalling can reorder the
 *  wire. When an unconfirmed request finally settles, its outcome still
 *  flows through the settles below, possibly long after newer picks were
 *  begun and answered: that is why the adjudication half exists and is not
 *  redundant with serialization — outcomes can reach the registry out of
 *  ticket order even though the wire never does, and the registry still has
 *  to converge the store on the value the backend actually holds:
 *    - the newest request's success always writes (nothing newer exists);
 *    - an older success while the newest is still in flight is HELD, not
 *      dropped — if the newest then fails, the held value is what the backend
 *      is really running, and the failure settle hands it back to write;
 *    - an older success arriving after the newest already FAILED writes
 *      directly (same reasoning, other arrival order);
 *    - an older success never overwrites a newer one already written, and a
 *      superseded failure is a pure no-op.
 *  Dropping any of these would leave the chip on a value the backend is not
 *  running — exactly the lie this module exists to prevent.
 */

import { i18nT } from '../i18n/t'

export type SlotSwitchField = 'model' | 'project'

interface Entry {
  /** Ticket of the newest request begun for this slot+field. */
  seq: number
  /** Newest in-flight target ('' once the newest request settles). */
  pending: string
  /** How the newest request ended, once it has. */
  newestOutcome: 'inflight' | 'success' | 'failure'
  /** Highest ticket whose value has been written to the store. */
  bestWrittenSeq: number
  /** Newest superseded success HELD while the newest request is in flight:
   *  the backend applied it, but a newer request may still supersede it. */
  heldSuccess: { seq: number; value: string } | null
}

const entries = new Map<string, Entry>()

const keyOf = (field: SlotSwitchField, slot: string): string => field + ':' + slot

/** Register a new in-flight switch and return its ticket for the settle calls. */
function beginSlotSwitch(field: SlotSwitchField, slot: string, target: string): number {
  const key = keyOf(field, slot)
  const entry = entries.get(key)
    ?? { seq: 0, pending: '', newestOutcome: 'inflight' as const, bestWrittenSeq: 0, heldSuccess: null }
  entry.seq += 1
  entry.pending = target
  entry.newestOutcome = 'inflight'
  entries.set(key, entry)
  return entry.seq
}

/** The newest in-flight target for this slot+field, `''` when none. */
export function pendingSlotSwitch(field: SlotSwitchField, slot: string): string {
  return entries.get(keyOf(field, slot))?.pending || ''
}

/** Settle a ticket whose API call SUCCEEDED, with the server's stored value.
 *  True: the caller must write that value to the store. False: hold or
 *  discard per the adjudication model above — do not write.
 */
function settleSlotSwitchSuccess(
  field: SlotSwitchField, slot: string, seq: number, value: string,
): boolean {
  const entry = entries.get(keyOf(field, slot))
  if (!entry) return false
  if (seq === entry.seq) {
    // Newest request succeeded: authoritative, supersedes anything held.
    entry.pending = ''
    entry.newestOutcome = 'success'
    entry.bestWrittenSeq = seq
    entry.heldSuccess = null
    return true
  }
  if (entry.newestOutcome === 'inflight') {
    // The race is still live: hold the newest superseded success for the
    // newest request's failure settle.
    if (!entry.heldSuccess || entry.heldSuccess.seq < seq) {
      entry.heldSuccess = { seq, value }
    }
    return false
  }
  if (entry.newestOutcome === 'failure' && seq > entry.bestWrittenSeq) {
    // The newest request failed (changed nothing server-side) and this late
    // success is the newest value that actually landed: write it.
    entry.bestWrittenSeq = seq
    return true
  }
  // A newer success has already been written — this one is history.
  return false
}

/** Settle a ticket whose API call FAILED.
 *
 *  Returns the value the caller should write to the store anyway, or `''`
 *  for none. Non-empty exactly when this failure was the NEWEST request and
 *  an older request's success was held in its favour: that held value is
 *  what the backend is actually running, so the store must adopt it or the
 *  chip keeps the pre-switch value forever (offline). A superseded failure
 *  is a pure no-op — a failed call changed nothing server-side.
 */
function settleSlotSwitchFailure(field: SlotSwitchField, slot: string, seq: number): string {
  const entry = entries.get(keyOf(field, slot))
  if (!entry || seq !== entry.seq) return ''
  entry.pending = ''
  entry.newestOutcome = 'failure'
  const held = entry.heldSuccess
  entry.heldSuccess = null
  if (held && held.seq > entry.bestWrittenSeq) {
    entry.bestWrittenSeq = held.seq
    return held.value
  }
  return ''
}

const chains = new Map<string, Promise<unknown>>()

/** How long a caller waits for its pick to confirm before being told it did
 *  not (measured from the pick, queue wait included). A switch POST to a
 *  healthy gateway answers in well under a second; a pick still unconfirmed
 *  after this long is behind a wedged connection. The budget bounds only the
 *  CALLER's wait — the wire call keeps its place in the chain (see the
 *  module header for why the chain must never be advanced early). */
export const SWITCH_CONFIRM_TIMEOUT_MS = 15_000

/** Run `request` after every earlier chained request for the same slot+field
 *  has settled or timed out — at most one switch request per slot+field is
 *  ever knowingly in flight, which is what makes ticket order equal server
 *  processing order (see the module header). Callers begin their ticket
 *  BEFORE chaining, so the pending target is visible to the next keypress
 *  immediately, while the wire call waits its turn.
 */
function chainSlotSwitch<T>(
  field: SlotSwitchField, slot: string, request: () => Promise<T>,
): Promise<T> {
  const key = keyOf(field, slot)
  const prev = chains.get(key) ?? Promise.resolve()
  // A predecessor's failure is ITS caller's to handle (each call site catches
  // and settles its own ticket); the chain carries only ordering.
  const run = prev.catch(() => undefined).then(request)
  // The stored tail must never reject, or the next link would re-throw a
  // failure that was already handled downstream.
  chains.set(key, run.catch(() => undefined))
  return run
}

/** Sentinel for the confirmation race when the pick outwaits its budget. */
const CONFIRM_TIMEOUT = Symbol('slot-switch-confirm-timeout')

/** The one entry point call sites use: begin a ticket, run `request` in this
 *  slot+field's strictly ordered chain, and adjudicate the outcome into at
 *  most one `write`.
 *
 *  `request` resolves to the server's STORED value for the switch (the call
 *  site maps the endpoint's response before handing it over). `write`
 *  receives the adjudicated value to put in the store — the request's own on
 *  the authoritative path, or a recovered older success when this (newest)
 *  request failed after an older one landed.
 *
 *  Rejects when the pick did not confirm: the request failed, or it did not
 *  settle within SWITCH_CONFIRM_TIMEOUT_MS. The timeout releases only the
 *  CALLER (so the failure toast can say the pick is unconfirmed instead of
 *  the picker freezing silently); the wire call keeps its place in the
 *  chain, and whenever it settles, its outcome is adjudicated exactly as if
 *  the caller were still waiting — a late success is written when it is what
 *  the backend was left running.
 */
export async function performSlotSwitch(
  field: SlotSwitchField,
  slot: string,
  target: string,
  request: () => Promise<string>,
  write: (value: string) => void,
): Promise<void> {
  const seq = beginSlotSwitch(field, slot, target)
  // The wire outcome ALWAYS adjudicates, whether or not the caller is still
  // waiting when it lands — this is the only path that touches the settles,
  // so a caller released by the timeout cannot race a second settle in.
  const adjudicated = chainSlotSwitch(field, slot, request).then(
    (value) => {
      if (settleSlotSwitchSuccess(field, slot, seq, value)) write(value)
      return { ok: true as const, value }
    },
    (error) => {
      const recovered = settleSlotSwitchFailure(field, slot, seq)
      if (recovered) write(recovered)
      return { ok: false as const, error }
    },
  )
  let timer: ReturnType<typeof setTimeout> | undefined
  const budget = new Promise<typeof CONFIRM_TIMEOUT>((res) => {
    timer = setTimeout(() => res(CONFIRM_TIMEOUT), SWITCH_CONFIRM_TIMEOUT_MS)
  })
  try {
    const outcome = await Promise.race([adjudicated, budget])
    if (outcome === CONFIRM_TIMEOUT) {
      // Unconfirmed is NOT failed: the pick may still land and apply. The
      // thrown message says exactly that (the shared notice helper prefers a
      // non-empty error message over its generic fallback), so the toast
      // never claims a failure the backend may yet contradict.
      throw new Error(i18nT('components.chatInput.switch_not_confirmed'))
    }
    if (!outcome.ok) throw outcome.error
  } finally {
    if (timer !== undefined) clearTimeout(timer)
  }
}
