/** The `slot_fold` frame: a work board's advance as a delta on a base revision.
 *
 *  A gateway sends it only to a tab that declared `fold_delta.2` in `?caps=`
 *  (see `connection.ts`) and watches the board, INSTEAD of that board's
 *  `slot_projection` frame. A gateway that predates it never sends it, so the
 *  tab keeps taking whole values through `slotProjection.ts`, as before.
 *
 *  THE BASE. The cached board (`['command-center', root, 'work']`) carries
 *  `published_revision`: the revision of the last publish the value is built on.
 *  The REST read returns it (the gateway folds the board first, outside its
 *  publish lock, then checks the value's revision against the last publish under
 *  that lock, so the label is always the revision of the value it returns), and
 *  each applied delta moves it to the frame's revision. A delta whose `base_revision` is exactly that number is applied;
 *  one at or below it is a duplicate; anything else, a `snapshot_needed` frame,
 *  and a frame version this bundle does not know, pull one snapshot (a re-read
 *  of the board). Revisions belong to the gateway process that minted them, so
 *  a new socket forgets every base (`resetWorkDeltaBases`) and the next read
 *  labels the board afresh.
 *
 *  While a read of the board is in flight, frames are HELD and replayed in order
 *  once it settles, by the same rule: those at or below the read's base drop,
 *  the one on it applies.
 *
 *  `applyWorkDelta` is the TypeScript twin of `apply_work` in
 *  `src/kiro_crew/crew_log/fold_delta.py`; both run the fixtures in
 *  `website/src/test/fixtures/work-delta-cases.json`. */
import type { QueryClient } from '@tanstack/react-query'
import { slotKey } from '../../pages/chat/command-center/model'

/** The frame type. */
export const SLOT_DELTA_FRAME = 'slot_fold'

/** The frame schema version this bundle reads. */
const FRAME_VERSION = 1

type Mapping = Record<string, unknown>

function isMapping(value: unknown): value is Mapping {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function itemId(item: unknown): string {
  if (!isMapping(item)) return ''
  const id = item.item_id
  return typeof id === 'string' && id ? id : ''
}

/** *value* with *ops* applied, or `null` when the ops do not fit *value*.
 *
 *  The result holds EXACTLY the ids in `header.order`: each from `items` when
 *  sent, else from *value* unless listed in `remove`. An id `order` names that
 *  is in neither is a hole, answered with `null` so the caller pulls a snapshot.
 *  *value* is never mutated. */
export function applyWorkDelta(value: unknown, ops: unknown): Mapping | null {
  if (!isMapping(value) || !isMapping(ops)) return null
  const { header, items, remove } = ops
  if (!isMapping(header) || !isMapping(items) || !Array.isArray(remove)) return null
  const order = header.order
  if (!Array.isArray(order)) return null
  const heldItems = value.items
  if (!Array.isArray(heldItems)) return null
  const held = new Map<string, unknown>()
  for (const item of heldItems) {
    const id = itemId(item)
    if (!id || held.has(id)) return null
    held.set(id, item)
  }
  const gone = new Set(remove)
  const next: unknown[] = []
  for (const id of order) {
    if (typeof id === 'string' && Object.prototype.hasOwnProperty.call(items, id)) next.push(items[id])
    else if (typeof id === 'string' && held.has(id) && !gone.has(id)) next.push(held.get(id))
    else return null
  }
  const result: Mapping = {}
  for (const [key, part] of Object.entries(header)) if (key !== 'order') result[key] = part
  result.items = next
  return result
}

/** One `slot_fold` frame after the shape check. */
export type SlotFoldFrame = {
  slot: string
  fold: string
  kind: 'delta' | 'snapshot_needed'
  revision: number
  baseRevision: number
  ops?: Mapping
}

function positive(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
}

/** Read a frame's `data`, or `null` when it names no board. A frame that cannot
 *  be applied as a delta (unknown `v`, bad ops, no base) reads as
 *  `snapshot_needed`, so the board is re-read rather than drawn wrong. */
export function readSlotFoldFrame(data: Mapping): SlotFoldFrame | null {
  const { v, slot, fold, kind, revision, base_revision: base, ops } = data
  if (typeof slot !== 'string' || !slot || typeof fold !== 'string' || !fold) return null
  const own = slotKey(slot)
  const rev = positive(revision) ? revision : 0
  if (v === FRAME_VERSION && kind === 'delta' && rev > 0 && positive(base) && isMapping(ops)) {
    return { slot: own, fold, kind: 'delta', revision: rev, baseRevision: base, ops }
  }
  return { slot: own, fold, kind: 'snapshot_needed', revision: rev, baseRevision: 0 }
}

/** What the frame did to the tab's board.
 *
 *  `applied`: the board moved, no read needed. `stale`: a duplicate, nothing to
 *  do. `held`: a read is in flight; the frame replays when it settles.
 *  `snapshot`: the board must be re-read. `ignored`: not a board this tab
 *  caches by delta (another fold). */
export type SlotFoldOutcome = 'applied' | 'stale' | 'held' | 'snapshot' | 'ignored'

function workKey(root: string): readonly unknown[] {
  return ['command-center', root, 'work']
}

/** Frames held per board while its read is in flight. */
const heldFrames = new Map<string, SlotFoldFrame[]>()

/** Forget every board's base and every held frame. Called when a socket opens:
 *  the next gateway's revisions are not comparable with this one's. */
export function resetWorkDeltaBases(queryClient: QueryClient): void {
  heldFrames.clear()
  // Only the entries that hold a base are rewritten, so a tab that never took a
  // delta (an older gateway) sees no cache write on connect.
  for (const query of queryClient.getQueryCache().findAll({ queryKey: ['command-center'] })) {
    const held = query.state.data
    if (!isMapping(held) || !('published_revision' in held)) continue
    const { published_revision: _dropped, ...rest } = held
    queryClient.setQueryData(query.queryKey, rest)
  }
}

/** Apply *frame* to the tab's cached board by the base rule.
 *
 *  *onReplay* is called with the outcome of each held frame replayed after a
 *  read, so the caller can pull a snapshot for one that needs it. */
export function applySlotFoldFrame(
  queryClient: QueryClient,
  frame: SlotFoldFrame,
  onReplay?: (frame: SlotFoldFrame, outcome: SlotFoldOutcome) => void,
): SlotFoldOutcome {
  if (frame.fold !== 'work') return 'ignored'
  const key = workKey(frame.slot)
  const query = queryClient.getQueryCache().find({ queryKey: key, exact: true })
  if (query?.state.fetchStatus === 'fetching' && query.promise) {
    const pending = heldFrames.get(frame.slot)
    if (pending) {
      pending.push(frame)
    } else {
      heldFrames.set(frame.slot, [frame])
      void query.promise.catch(() => undefined).finally(() => {
        const frames = heldFrames.get(frame.slot) ?? []
        heldFrames.delete(frame.slot)
        for (const next of frames) {
          const outcome = applySlotFoldFrame(queryClient, next, onReplay)
          onReplay?.(next, outcome)
        }
      })
    }
    return 'held'
  }
  const held = queryClient.getQueryData(key)
  const base = isMapping(held) && positive(held.published_revision) ? held.published_revision : 0
  if (base > 0 && frame.revision > 0 && frame.revision <= base) return 'stale'
  if (frame.kind !== 'delta' || base <= 0 || frame.baseRevision !== base || !isMapping(held)) {
    return 'snapshot'
  }
  const applied = applyWorkDelta(held.value, frame.ops)
  if (!applied) return 'snapshot'
  queryClient.setQueryData(key, { ...held, value: applied, published_revision: frame.revision })
  return 'applied'
}
