import { useMutation } from '@tanstack/react-query'
import { api } from '../api/client'
import { JEV_ROUTE_MODEL } from '../lib/jevRoute'
import { useAppDispatch, useAppSelector } from '../store'
import { updateSlot } from '../store/dashboardSlice'
import type { JevRange } from '../types'
import type { ModelItem } from './ModelDropdownList'

const NO_RANGE: JevRange = { models: [], effort_min: '', effort_max: '' }
// The latest save per slot, shared by every picker mount. A reply lands from the mutation itself, so it
// still reaches its slot after the picker closed or remounted, and a slower earlier reply cannot overwrite a newer range.
const latest: Record<string, unknown> = {}

export type JevRangeEdit = NonNullable<ReturnType<typeof useJevRange>>

/** The slot's Jev range as a model picker edits it, or null when the picker offers
 *  none: only with `Auto (Jev)` picked, and not while a filter hides rows, where an
 *  unticked row would post only the visible ids. A refused save falls back to the
 *  stored range. */
export function useJevRange(slot: string | null, models: ModelItem[], activeModel: string, filter: string) {
  const dispatch = useAppDispatch()
  const stored = useAppSelector(s => s.dashboard.slots.find(x => x.key === slot)?.jev_range) ?? NO_RANGE
  const save = useMutation({
    mutationFn: (v: { slot: string; next: JevRange }) => api.chatSlotJevRange(v.slot, v.next),
    onMutate: v => { latest[v.slot] = v },
    onSuccess: (r, v) => { if (latest[v.slot] === v) dispatch(updateSlot({ key: v.slot, jev_range: r.jev_range })) },
  })
  if (!slot || filter || activeModel !== JEV_ROUTE_MODEL || !models.some(m => m.name === JEV_ROUTE_MODEL)) return null

  const range = (save.isPending && save.variables.slot === slot && save.variables.next) || stored
  const listed = models.map(m => m.name).filter(name => name !== 'auto' && name !== JEV_ROUTE_MODEL)
  // A stored id the list no longer shows would be refused on every save. A range of only
  // such ids is posted as it is: an empty list would mean every model.
  const set = (next: JevRange) => {
    const shown = next.models.filter(id => listed.includes(id))
    save.mutate({ slot, next: { ...next, models: shown.length > 0 ? shown : next.models } })
  }
  const allowed = range.models.length > 0 ? range.models.filter(id => listed.includes(id)) : listed
  return {
    range,
    set,
    error: save.variables?.slot === slot ? save.error : null,
    allows: (id: string) => allowed.includes(id),
    // An empty list means every model, so the last ticked row cannot be unticked.
    locked: (id: string) => allowed.length === 1 && allowed[0] === id,
    toggle: (id: string) => set({
      ...range,
      models: allowed.includes(id) ? allowed.filter(m => m !== id) : [...allowed, id],
    }),
  }
}
