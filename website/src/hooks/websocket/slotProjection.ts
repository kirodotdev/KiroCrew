/** The `slot_projection` frame, in both shapes the server sends it.
 *
 *  `{slot}` alone is the original growth signal: a slot's crew log moved and
 *  this tab must re-read the folds it shows for that board.
 *
 *  `{slot, fold, revision, value}` is what an EAGER advance sends. The value is
 *  already folded, so the frame REPLACES the read instead of prompting one.
 *
 *  WHY A REVISION AND NOT A SEQ. A slot fold joins several unit files and its
 *  `seq` is the newest unit's own, so a conductor-side change on a board with a
 *  worker bound leaves that number unmoved — ordering frames by it would discard
 *  the changed value. The server mints a monotonic per-(slot, fold) revision
 *  instead, so the rule here is simply "keep the highest, discard anything
 *  lower". The counter belongs to the process that folded, so a reconnect to
 *  another gateway can see a LOWER revision than this tab holds;
 *  `resetSlotProjectionRevisions` is what the connection calls so a fresh socket
 *  is not judged against the previous one's numbering.
 *
 *  THE REVISION ORDERS FRAMES AND NOTHING ELSE. It never gates a READ: a REST
 *  response is applied whatever revision it carries, so the newest read always
 *  wins. A briefly wrong board is acceptable — whoever reads it notices, and the
 *  panel's own pull re-reads — while a board that can be held behind a number
 *  indefinitely is not acceptable, which is what gating reads against a floor
 *  costs. The ledger here holds only revisions this tab ACTUALLY APPLIED from a
 *  frame, so a value it never received can never be below its own floor. */
import type { QueryClient } from '@tanstack/react-query'
import { slotKey } from '../../pages/chat/command-center/model'

/** One valued frame, after the shape check and the revision gate. */
export type FoldedSlotProjection = {
  slot: string
  fold: string
  revision: number
  value: Record<string, unknown>
}

/** The highest revision APPLIED FROM A FRAME, per slot and then per fold.
 *
 *  Written only by `takeFoldedSlotProjection`, and read only by it: a number
 *  lands here exactly when this tab takes the value that carries it. Nothing the
 *  server merely announces is recorded, because a floor above a value this tab
 *  never received would drop the frame that finally delivers it.
 *
 *  NESTED rather than keyed by a joined string, so no separator has to be chosen
 *  that a slot key cannot contain. Module-level and not React state: the gate
 *  must answer before any render, and two frames of one board can arrive in a
 *  single tick. */
const accepted = new Map<string, Map<string, number>>()

/** Forget every accepted revision. Called when a socket opens.
 *
 *  A revision is only comparable within the gateway process that minted it, so
 *  carrying this tab's numbers across a reconnect could make every frame from a
 *  restarted gateway look stale and leave the board frozen until a manual
 *  re-read. */
export function resetSlotProjectionRevisions(): void {
  accepted.clear()
}

/** What a `slot_projection` frame turned out to be.
 *
 *  THREE outcomes and not two, because "no value to apply" is two different
 *  instructions. `bare` is the growth signal, which still needs the re-read it
 *  always needed. `stale` is a valued frame this tab has already passed, and the
 *  correct response to it is NOTHING — falling back to the re-read there would
 *  spend the request the revision exists to save. */
export type SlotProjectionFrame =
  | { kind: 'bare' }
  | { kind: 'stale' }
  | { kind: 'folded'; frame: FoldedSlotProjection }

/** Read one frame and decide what to do with it.
 *
 *  ONE function for the shape check and the revision gate, because a caller
 *  holding a frame that passed one but not the other has nothing to do with it,
 *  and because recording the revision has to happen WITH the decision: split in
 *  two, a pair of frames for one board in the same tick would both pass.
 *
 *  Every field is checked, because this is wire data: a fold that is not a
 *  string, a revision that is not a positive number, or a value that is not an
 *  object makes the frame unusable AS A VALUE, so it is read as `bare` and the
 *  re-read covers it.
 *
 *  Equal revisions are STALE — the server mints a fresh revision for every
 *  advance and keeps the old one for a cell that folded nothing, so a repeat is a
 *  duplicate delivery and never a new value. */
export function takeFoldedSlotProjection(data: Record<string, unknown>): SlotProjectionFrame {
  const { slot, fold, revision, value } = data as {
    slot?: unknown; fold?: unknown; revision?: unknown; value?: unknown
  }
  if (typeof slot !== 'string' || !slot) return { kind: 'bare' }
  if (typeof fold !== 'string' || !fold) return { kind: 'bare' }
  if (typeof revision !== 'number' || !Number.isFinite(revision) || revision <= 0) {
    return { kind: 'bare' }
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return { kind: 'bare' }
  // In the dashboard's own key form (`slotKey`), which is what a board's query
  // key uses: the server may name the slot either way, and a revision recorded
  // under the other spelling is one nothing reads.
  const own = slotKey(slot)
  let folds = accepted.get(own)
  if (!folds) accepted.set(own, (folds = new Map()))
  const held = folds.get(fold)
  if (held !== undefined && revision <= held) return { kind: 'stale' }
  folds.set(fold, revision)
  return { kind: 'folded', frame: { slot: own, fold, revision, value: value as Record<string, unknown> } }
}

/** The query key a fold's value belongs to, or `null` when this tab caches none.
 *
 *  Only `work` is cached by fold; `ledger`, `radar` and `panel` are read through
 *  their own routes with their own keys. A fold with no key here is not an error
 *  — its frame is simply not seeded, and its own reader still reads. */
function foldQueryKey(root: string, fold: string): readonly unknown[] | null {
  return fold === 'work' ? ['command-center', root, 'work'] : null
}

/** Whether a read is already in flight for the key *frame* would seed.
 *
 *  A push must NOT seed across one. A REST response already on the wire resolves
 *  into the same cache entry and overwrites whatever is there, so a value written
 *  now is replaced by an OLDER one and nothing is left to correct it. The caller's
 *  answer to `true` is to take the bare growth signal's path instead, which waits
 *  for that promise and invalidates after it settles.
 *
 *  The frame's OWN board only: that is the one key a push seeds. */
export function fetchingAnyFoldQuery(queryClient: QueryClient, frame: FoldedSlotProjection): boolean {
  const key = foldQueryKey(frame.slot, frame.fold)
  if (!key) return false
  const held = queryClient.getQueryCache().find({ queryKey: key, exact: true })
  return held?.state.fetchStatus === 'fetching'
}

/** Seed *frame*'s value into *root*'s cache; whether that key is now current (holds
 *  this revision or a newer one), so the caller need not re-read it. *root* must be
 *  `frame.slot`: the value is that board's, and no other root's.
 *
 *  MERGED into whatever the key already holds rather than written over it, so a
 *  field the REST shape carries and the frame does not (`name`, `seq`) survives.
 *  An absent entry is seeded whole, because a tab that has not read yet is
 *  exactly the one a push saves a read for.
 *
 *  NEVER BACKWARDS. The cached entry carries a `revision` only when a FRAME wrote
 *  it: the work read strips its own revision before caching (see useCommandCenter),
 *  because a read revision is not comparable to a frame revision. So the check
 *  below compares a frame against an earlier FRAME, never against a read: a
 *  redelivered or out-of-order frame older than one already applied is dropped,
 *  while a read never raises the floor and the next frame always applies. The
 *  check is made against the CACHED value, which is the only thing that knows.
 *  When the held value's revision is at or above the frame's, it stays, and the
 *  key is already current. A `work` value a REST read labelled with
 *  `published_revision` (a gateway publish revision, comparable to a frame's)
 *  stays the same way; when a newer frame does replace it, the label is dropped,
 *  because it named the old value and a delta on it would apply to the wrong board.
 *
 *  The caller owes `fetchingAnyFoldQuery` first: an in-flight read is not yet held. */
export function seedFoldedProjection(
  queryClient: QueryClient,
  root: string,
  frame: FoldedSlotProjection,
): boolean {
  const key = foldQueryKey(root, frame.fold)
  if (!key) return false
  queryClient.setQueryData(key, (held: unknown) => {
    const current = typeof held === 'object' && held !== null ? held as Record<string, unknown> : undefined
    const atOrAbove = (n: unknown) => typeof n === 'number' && Number.isFinite(n) && n >= frame.revision
    if (atOrAbove(current?.revision) || atOrAbove(current?.published_revision)) {
      return held
    }
    // The held `published_revision` labels the value being replaced, not this one,
    // so it goes: the next `slot_fold` delta then has no base and pulls a snapshot.
    const { published_revision: _dropped, ...rest } = current ?? {}
    return { ...rest, value: frame.value, revision: frame.revision }
  })
  return true
}
