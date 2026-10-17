/** The `session_projection` frame: one SESSION fold the gateway folded eagerly.
 *
 *  The crew-log panel reads every session fold in one REST call, cached under
 *  `['crew-log-projections', slot]`. The gateway now folds those as entries land
 *  and pushes each one that moved, carrying its value and a REVISION. This module
 *  is the one place a frame is applied to that cache.
 *
 *  FOUR outcomes, because each needs a different action from the caller:
 *  - `applied`: the frame's value replaced the held fold.
 *  - `stale`: the tab already holds this revision or a newer one. Dropped.
 *  - `refetch`: the frame cannot be applied, but says the panel moved -- it names
 *    another unit than the cached read, or a read is in flight that would land
 *    over a seeded value. The caller invalidates, after any in-flight read settles.
 *  - `ignored`: nothing caches this slot, or the frame is malformed. A tab with
 *    no panel open has nothing to keep current; it reads on mount.
 *
 *  ORDERED BY REVISION, never by seq. A seq restarts when a unit is recreated
 *  under the same id; a revision is minted by the gateway and only rises. */
import type { QueryClient } from '@tanstack/react-query'

import { slotKey } from '../../pages/chat/command-center/model'

/** The cached read's shape, as far as this module touches it. */
type HeldRead = {
  unit?: string
  folds: Record<string, { revision?: number; seq?: number; value?: unknown } | undefined>
}

/** One frame after the shape check. */
export type SessionProjectionFrame = {
  unit: string
  slot: string
  name: string
  seq: number
  value: Record<string, unknown>
  revision: number
}

export type SessionProjectionOutcome = 'applied' | 'stale' | 'refetch' | 'ignored'

/** The panel's query key for *slot*. One spelling, shared with the reader.
 *
 *  In the dashboard's own key form (`slotKey`): the panel reads under the bare
 *  slot, while a frame names it scope-qualified (`dashboard:<slot>`). */
export function crewLogProjectionsKey(slot: string): readonly unknown[] {
  return ['crew-log-projections', slotKey(slot)]
}

/** *data* as a frame, or `null` when any field is missing or the wrong type. */
export function readSessionProjectionFrame(data: Record<string, unknown>): SessionProjectionFrame | null {
  const { session_id: unit, slot, name, seq, value, revision } = data
  if (typeof unit !== 'string' || !unit) return null
  if (typeof slot !== 'string' || !slot) return null
  if (typeof name !== 'string' || !name) return null
  if (typeof seq !== 'number' || !Number.isFinite(seq)) return null
  if (typeof revision !== 'number' || !Number.isFinite(revision) || revision <= 0) return null
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  return { unit, slot, name, seq, value: value as Record<string, unknown>, revision }
}

/** Apply one frame to the panel's cache; what happened. See the module header. */
export function applySessionProjection(
  queryClient: QueryClient,
  frame: SessionProjectionFrame,
): SessionProjectionOutcome {
  const key = crewLogProjectionsKey(frame.slot)
  const query = queryClient.getQueryCache().find({ queryKey: key, exact: true })
  const held = query?.state.data as HeldRead | undefined
  if (!query || held === undefined) return 'ignored'
  // A read on the wire resolves into this entry LATER and would replace a value
  // written now with an older one. Let it land, then read once more.
  if (query.state.fetchStatus === 'fetching') return 'refetch'
  // Another unit than the one the cached read folded: the slot moved to a new
  // ACP session, or this is a late frame from the old one. Neither may be merged
  // into this read; a fresh read decides.
  if (held.unit && held.unit !== frame.unit) return 'refetch'
  const current = held.folds?.[frame.name]
  // A fold the read does not carry is one this panel does not draw.
  if (current === undefined) return 'ignored'
  if ((current.revision ?? 0) >= frame.revision) return 'stale'
  queryClient.setQueryData<HeldRead>(key, prev => prev && {
    ...prev,
    folds: {
      ...prev.folds,
      [frame.name]: { ...current, name: frame.name, seq: frame.seq, value: frame.value, revision: frame.revision },
    },
  })
  return 'applied'
}

/** The caller's half of `refetch`: invalidate, after any in-flight read settles. */
export function refetchSessionProjections(queryClient: QueryClient, slot: string): void {
  const key = crewLogProjectionsKey(slot)
  const query = queryClient.getQueryCache().find({ queryKey: key, exact: true })
  if (query?.state.fetchStatus === 'fetching' && query.promise) {
    void query.promise
      .catch(() => undefined)
      .finally(() => { void queryClient.invalidateQueries({ queryKey: key, exact: true }, { cancelRefetch: false }) })
    return
  }
  void queryClient.invalidateQueries({ queryKey: key, exact: true }, { cancelRefetch: false })
}
