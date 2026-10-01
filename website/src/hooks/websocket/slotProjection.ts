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
 *  is not judged against the previous one's numbering. */
import type { QueryClient } from '@tanstack/react-query'
import { slotKey } from '../../pages/chat/command-center/model'

/** One valued frame, after the shape check and the revision gate. */
export type FoldedSlotProjection = {
  slot: string
  fold: string
  revision: number
  value: Record<string, unknown>
}

/** The highest revision accepted, per slot and then per fold.
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

/** Re-base this tab's revision floors on `slot_projection/subscribed`.
 *
 *  THE ORDER IS THE WHOLE POINT. The server sends this before the client issues
 *  any baseline read, so the floor is in place first. A baseline read already on
 *  the wire resolves into the same cache entry a pushed frame writes, and it
 *  resolves LATER — so without a floor the older response wins by arriving last.
 *
 *  REPLACED, never merged. A revision orders frames within ONE gateway process:
 *  a restarted gateway mints from 1 again, so a floor this tab kept from the old
 *  process would sit above every new frame and drop it as stale. The subscribe
 *  frame is the serving process's own truth, so it becomes the whole ledger.
 *
 *  Taken as a FLOOR and not as data: it carries revisions, never folded values,
 *  so this tab still reads its own baseline. Shape-checked per entry, because
 *  this is wire data and a bad `revisions` map must cost the floor rather than
 *  the connection. */
export function recordSlotProjectionFloor(data: Record<string, unknown>): void {
  const revisions = (data as { revisions?: unknown }).revisions
  if (typeof revisions !== 'object' || revisions === null || Array.isArray(revisions)) return
  accepted.clear()
  for (const [slot, folds] of Object.entries(revisions as Record<string, unknown>)) {
    if (!slot || typeof folds !== 'object' || folds === null || Array.isArray(folds)) continue
    const own = slotKey(slot)
    for (const [fold, revision] of Object.entries(folds as Record<string, unknown>)) {
      if (!fold || typeof revision !== 'number' || !Number.isFinite(revision) || revision <= 0) continue
      let held = accepted.get(own)
      if (!held) accepted.set(own, (held = new Map()))
      const current = held.get(fold)
      if (current === undefined || revision > current) held.set(fold, revision)
    }
  }
}

/** The floor this tab holds for one (slot, fold); `0` when it holds none.
 *
 *  What a baseline reader checks a response against: a response carrying a
 *  revision at or below this is older than something already applied here. */
export function slotProjectionFloor(slot: string, fold: string): number {
  return accepted.get(slotKey(slot))?.get(fold) ?? 0
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
  // key and its baseline reader use: the server may name the slot either way,
  // and a floor recorded under the other spelling is a floor nothing reads.
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

/** One baseline response, or the value this tab already holds when it is NEWER.
 *
 *  The second half of the subscribe-before-baseline rule. The floor says which
 *  revision this tab has already applied; a response at or below it describes an
 *  older fold, and letting it become the cache value would undo a push by
 *  arriving later.
 *
 *  A response AT the floor is accepted. A pushed frame raises the floor even when
 *  it could not be seeded (a read was in flight), so the follow-up baseline that
 *  read exists to fetch carries exactly that revision -- refusing it would keep
 *  the older value standing with nothing left to correct it.
 *
 *  A COLD response below the floor (nothing held) is neither accepted nor
 *  held: it throws `StaleBaselineError`, and the query's retry reads again.
 *
 *  A response with NO revision is accepted. That is a gateway that pushes nothing
 *  — an older build, or the crew log switched off — and refusing its reads would
 *  leave the board empty rather than merely stale.
 *
 *  Pure: it reads the floor and returns one of its two inputs, so a caller can
 *  use it as the tail of a `queryFn` without a cache write of its own. */
export function baselineOrHeld<T>(slot: string, fold: string, response: T, held: T | undefined): T {
  const revision = (response as { revision?: unknown } | null)?.revision
  if (typeof revision !== 'number' || !Number.isFinite(revision)) return response
  const floor = slotProjectionFloor(slot, fold)
  if (revision >= floor) return response
  // Below the floor. A held value AT or above it is kept. Anything else -- nothing
  // held, or a held value that is itself below the floor -- would serve a value
  // older than one already announced, so keep nothing and throw, which the query
  // client answers with one retry: a fresh read of the current fold.
  if (heldRevision(held) >= floor) return held as T
  throw new StaleBaselineError(slot, fold, revision)
}

/** *held*'s revision, or `0` when it carries none (or nothing is held). */
function heldRevision(held: unknown): number {
  const revision = (held as { revision?: unknown } | null | undefined)?.revision
  return typeof revision === 'number' && Number.isFinite(revision) ? revision : 0
}

/** Invalidate every cached board whose held revision is below its cell's floor.
 *
 *  THE INVARIANT this module keeps: a cached projection value below the floor
 *  recorded for its cell is never served. A pushed frame keeps it by seeding the
 *  key or re-reading it, and a baseline read by `baselineOrHeld`. A floor written
 *  any other way -- the subscribe frame -- can land on a value a read already
 *  cached, with nothing else left to move it, so whoever writes such a floor calls
 *  this right after. Re-read rather than dropped, so the board keeps showing its
 *  value until the current one arrives. */
export function invalidateBelowFloor(queryClient: QueryClient): void {
  for (const query of queryClient.getQueryCache().findAll({ queryKey: ['command-center'] })) {
    const [, root, fold] = query.queryKey as unknown[]
    if (typeof root !== 'string' || fold !== 'work' || query.queryKey.length !== 3) continue
    if (query.state.data === undefined) continue
    if (heldRevision(query.state.data) >= slotProjectionFloor(root, 'work')) continue
    void queryClient.invalidateQueries({ queryKey: query.queryKey, exact: true }, { cancelRefetch: false })
  }
}

/** A cold baseline older than this tab's floor: thrown so the query reads again.
 *
 *  It carries its facts as fields and no message: it never reaches a screen (the
 *  query's retry consumes it), so there is no copy to write or translate. */
export class StaleBaselineError extends Error {
  readonly slot: string
  readonly fold: string
  readonly revision: number

  constructor(slot: string, fold: string, revision: number) {
    super()
    this.name = new.target.name
    this.slot = slot
    this.fold = fold
    this.revision = revision
  }
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
 *  NEVER BACKWARDS. A REST baseline raises no floor (only frames and the subscribe
 *  frame do), so a frame can pass the floor and still be older than the value the
 *  cache holds: a read at revision 10 answered before a delayed frame at 9. When the
 *  held value's revision is at or above the frame's, it stays, and the key is
 *  already current.
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
    const current = typeof held === 'object' && held !== null ? held : undefined
    const revision = (current as { revision?: unknown } | undefined)?.revision
    if (typeof revision === 'number' && Number.isFinite(revision) && revision >= frame.revision) {
      return held
    }
    return { ...(current ?? {}), value: frame.value, revision: frame.revision }
  })
  return true
}
