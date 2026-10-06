/** The model vocabulary the sidebar filter offers: every model at least one
 *  session uses, grouped by canonical key, with the lookups the menu rows, the
 *  chip and the filter predicate read. */
import { useMemo } from 'react'
import { i18nT } from '../../i18n/t'
import { normalizeModelKey } from '../../lib/model'
import { ROUTING_PREFIX_RE } from '../../providers/modelRegistry'
import type { Slot } from './types'
import { isPeerRow } from './rowIdentity'

/** The key sessions with no pin, or an explicit `auto`, group under. One row:
 *  both run whatever the backend picks, and the filter asks which model RUNS a
 *  session, not whether the person pinned it (`normalizeModelKey` keeps the two
 *  apart because the slot display needs that distinction; this filter does not). */
export const DEFAULT_MODEL_FILTER_KEY = 'auto'

/** The filter key a slot's model falls under: the STORED PIN, and only that.
 *  The filter exists to find the sessions still pinned to a model so they can be
 *  moved, so two things the composer folds into `auto` stay apart here:
 *  - a pin the account can no longer run (`model_withheld: true`, a retired or
 *    unentitled model) is exactly such a session and keeps its own row;
 *  - the model a pinless session happens to be served (`served_model`) is not
 *    used, because a session that follows the default moves by itself.
 *  Both fields also change with runtime state (a verdict exists only while a
 *  session is live), so reading them would make the counts shift with nothing
 *  the person did. */
export function slotModelKey(slot: Pick<Slot, 'model'>): string {
  return normalizeModelKey(slot.model ?? '') || DEFAULT_MODEL_FILTER_KEY
}

export interface ModelFilterRow {
  key: string
  /** The first spelling seen for this key, for the label of an unregistered model. */
  rawId: string
  count: number
  selected: boolean
}

/** The row's label, resolved at render time so a language change re-resolves
 *  the Default row. A model row carries the id as the session does, minus a
 *  routing prefix: the composer footer and the assistant header label a model
 *  by id, so the filter must name it the same way or a session filtered as one
 *  name opens under another. */
export function modelFilterLabel(row: Pick<ModelFilterRow, 'key' | 'rawId'>): string {
  if (row.key === DEFAULT_MODEL_FILTER_KEY) return i18nT('pages.chatSidebar.model_default_label')
  return row.rawId.replace(ROUTING_PREFIX_RE, '')
}

/** The models in use and the lookups built from them. */
export function useSidebarModels({ filterModelKeys, filterModelsExcluded, localSlots }: {
  filterModelKeys: Set<string>
  filterModelsExcluded: boolean
  localSlots: Slot[]
}) {
  /** Rows for the filter menu's Models section, most-used first, so the section
   *  doubles as a read of what the fleet runs on. Counts come from all
   *  `localSlots`, not the filtered list, for the same reason the tag counts do:
   *  the number a person consults is the one BEFORE selecting the row. A
   *  vocabulary derived from the sessions themselves lists only models in use;
   *  the registry's full catalog would add rows that can only blank the list.
   *  Being live, the order can shift while the menu is open if a session is
   *  created or closed; the tag section cannot, since its vocabulary order is
   *  fixed. Accepted: "most used first" is the point of the section. */
  const modelFilterRows = useMemo<ModelFilterRow[]>(() => {
    const byKey = new Map<string, { rawId: string; count: number }>()
    for (const slot of localSlots) {
      const key = slotModelKey(slot)
      const rawId = slot.model ?? ''
      const entry = byKey.get(key)
      if (!entry) byKey.set(key, { rawId, count: 1 })
      else {
        entry.count += 1
        // Smallest spelling wins, so the label of an unregistered model depends
        // on the SET of spellings, not on which slot happens to sort first.
        if (rawId < entry.rawId) entry.rawId = rawId
      }
    }
    return [...byKey.entries()]
      .map(([key, { rawId, count }]) => ({ key, rawId, count, selected: filterModelKeys.has(key) }))
      // Byte order on the canonical KEY as the tiebreak: it is a machine id
      // (`fable-5-1m`), not copy, so a locale-aware compare would buy nothing
      // and vary the menu order with the browser's language.
      .sort((a, b) => b.count - a.count || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0))
  }, [localSlots, filterModelKeys])
  /** Selected keys narrowed to models some session STILL uses. Once the last
   *  session on a model moves off it, its key stays in localStorage and would
   *  match nothing: in include mode that blanks the list with no row left to
   *  explain why, and in exclude mode it would keep a stale "Not X" chip on
   *  screen. Ignored rather than pruned, like the tag filter: a key absent while
   *  the slot list is still loading must not destroy a valid selection. */
  const activeModelKeys = useMemo(() => {
    const present = new Set(modelFilterRows.map(row => row.key))
    return new Set([...filterModelKeys].filter(key => present.has(key)))
  }, [modelFilterRows, filterModelKeys])
  /** Does the slot pass the model filter? The exclude flag inverts membership.
   *  A PEER row never passes a narrowing filter, in either direction: the peer
   *  slot allowlist (`_clean_peer_slot`) carries no `model`, so the row has no
   *  model datum, and an absent datum is not "Default" (which would show every
   *  peer session under the Default row) and not "not X" either. The tag filter
   *  hides a peer row the same way, since the allowlist carries no `tags`. */
  const modelFilterPasses = useMemo(
    () => (slot: Slot) => {
      if (activeModelKeys.size === 0) return true
      if (isPeerRow(slot)) return false
      return activeModelKeys.has(slotModelKey(slot)) !== filterModelsExcluded
    },
    [activeModelKeys, filterModelsExcluded],
  )
  /** The selected rows, in menu order, for the chip. */
  const activeModelRows = useMemo(
    () => modelFilterRows.filter(row => activeModelKeys.has(row.key)),
    [modelFilterRows, activeModelKeys],
  )
  return { modelFilterRows, activeModelKeys, activeModelRows, modelFilterPasses }
}
