/**
 * The one write path for the starred Bots' order (`star_order`), shared by the
 * left dock's drag (`shell/nav/matesDock.tsx`) and the Bots panel's drag and
 * Alt+Arrow move (`pages/members/rosterStarOrder.tsx`), so the two lists show
 * one order.
 *
 * A move renumbers the whole starred list and writes every row
 * (`starOrderWrites`), one `PUT /api/agents/{name}` at a time, after patching
 * the roster cache. Every reorder re-reads the roster when it settles, so a
 * list shows what the server holds after a partial failure too. A move from
 * either list is refused while any reorder is saving, its writes and that
 * re-read both: two renumberings at once would interleave their writes.
 */
import { useCallback } from 'react'
import { useIsMutating, useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type MemberRosterRow } from '../api/client'
import { MEMBERS_ROSTER_QUERY_KEY } from '../api/membersQuery'
import { starOrderWrites } from '../lib/dockMates'
import { haptic } from '../lib/haptic'

const STAR_ORDER_MUTATION_KEY = ['star-order'] as const

export function useStarOrderReorder() {
  const queryClient = useQueryClient()
  /** A reorder from either list is saving, for the list's `aria-busy`. */
  const busy = useIsMutating({ mutationKey: STAR_ORDER_MUTATION_KEY }) > 0
  const { mutate, error, reset } = useMutation({
    mutationKey: STAR_ORDER_MUTATION_KEY,
    // One write per moved row, in order: each is its own crew-record save
    // under the config lock, so running them in parallel would only queue.
    mutationFn: async (writes: { name: string; star_order: number }[]) => {
      for (const w of writes) await api.updateKirocrewAgent(w.name, { star_order: w.star_order })
    },
    onMutate: async (writes) => {
      await queryClient.cancelQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
      const byName = new Map(writes.map(w => [w.name, w.star_order]))
      queryClient.setQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY, rows =>
        rows?.map(r => (byName.has(r.name) ? { ...r, star_order: byName.get(r.name) } : r)))
    },
    // Some writes may have landed before a failure. The mutation stays pending
    // until this re-read settles, which is what keeps moves refused until the
    // roster has reconciled with the server on either outcome.
    onSettled: () => queryClient.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY }),
  })
  /** Read from the cache at the moment of a gesture: a render can lag a
   *  reorder started in the same tick, or by the other list. */
  const saving = useCallback(
    () => queryClient.isMutating({ mutationKey: STAR_ORDER_MUTATION_KEY }) > 0,
    [queryClient],
  )
  /** Move the row at `from` to `to` in `ordered`, the whole starred list in its
   *  current order. False when nothing is written: while a reorder is saving,
   *  or when the move changes nothing. */
  const move = useCallback(
    (ordered: readonly MemberRosterRow[], from: number, to: number) => {
      if (saving()) return false
      const writes = starOrderWrites(ordered, from, to)
      if (writes.length === 0) return false
      haptic('light')
      mutate(writes)
      return true
    },
    [saving, mutate],
  )
  return { busy, saving, move, error, reset }
}
