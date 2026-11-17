/** The "Started by" vocabulary the sidebar filter offers on a Shared Crew: You,
 *  each person, and Unattributed, with the lookups the menu rows, the chip and
 *  the filter predicate read. The filter runs on the rows the list already
 *  holds, from each row's own `attribution`, the same way the tag and model
 *  filters do. */
import { useMemo } from 'react'
import type { SessionAttribution } from '../../api/sharedCrew'
import { isUnknownPrincipal, principalName, type PeopleRow } from '../../api/sharedCrewUi'
import { i18nT } from '../../i18n/t'
import type { Slot } from './types'

/** The verified viewer's own sessions. */
export const CREATOR_MINE = 'mine'
/** Sessions with no human creator and no named trigger. */
export const CREATOR_UNATTRIBUTED = 'unattributed'

/** The "Started by" key a row falls under, or `null` when it falls under none.
 *  The same reading as the row's byline (`creatorDisplay`): the viewer's own
 *  session is `mine`, a person's is their principal id, and a creator nobody can
 *  name with no trigger is `unattributed`. A session a schedule or a webhook
 *  started names its trigger, so it is no person's and not Unattributed either.
 *  A row with no attribution (a peer instance's row, an older gateway) has no
 *  datum, so a narrowing filter never shows it.
 *  shortcut: `mine` matches the viewer's one principal id, as the byline's "You"
 *  does; the server's filter also folded the owner's other local ids into it.
 *  Fold them here once the mode route sends them. */
export function slotCreatorKey(attribution: SessionAttribution | null | undefined, viewerId: string): string | null {
  if (!attribution) return null
  const creator = attribution.creator
  if (!isUnknownPrincipal(creator) && creator.kind === 'human') return viewerId && creator.id === viewerId ? CREATOR_MINE : creator.id
  if (attribution.trigger) return null
  return isUnknownPrincipal(creator) ? CREATOR_UNATTRIBUTED : null
}

export interface CreatorFilterRow {
  key: string
  label: string
  count: number
  selected: boolean
}

/** The section's rows and the filter built from them. With `shown` false (a
 *  personal Crew, the preview off, nobody joined) there are no rows and the
 *  filter passes every row, whatever is stored. */
export function useSidebarCreators({ shown, selectedKeys, people, viewerId, rows }: {
  shown: boolean
  selectedKeys: ReadonlySet<string>
  people: readonly PeopleRow[] | undefined
  viewerId: string
  rows: Slot[]
}) {
  /** Counts come from every row, not the filtered list, as the tag and model
   *  counts do: the number a person consults is the one before ticking the row. */
  const counts = useMemo(() => {
    const byKey = new Map<string, number>()
    const names = new Map<string, string>()
    if (!shown) return { byKey, names }
    for (const slot of rows) {
      const key = slotCreatorKey(slot.attribution, viewerId)
      if (key === null) continue
      byKey.set(key, (byKey.get(key) ?? 0) + 1)
      if (key !== CREATOR_MINE && key !== CREATOR_UNATTRIBUTED && !names.has(key)) names.set(key, principalName(slot.attribution?.creator))
    }
    return { byKey, names }
  }, [shown, rows, viewerId])
  /** You first, then People in the order the People read gives, then anyone a
   *  row names that People does not list, then Unattributed. A person with no
   *  session is listed with 0: that row is the one that blanks the list. */
  const creatorFilterRows = useMemo<CreatorFilterRow[]>(() => {
    if (!shown) return []
    const out: CreatorFilterRow[] = []
    const push = (key: string, label: string) => {
      if (!label || out.some(row => row.key === key)) return
      out.push({ key, label, count: counts.byKey.get(key) ?? 0, selected: selectedKeys.has(key) })
    }
    push(CREATOR_MINE, i18nT('sharedCrew.you'))
    for (const row of people ?? []) {
      if (row.is_viewer || isUnknownPrincipal(row.principal) || row.principal.id === viewerId) continue
      push(row.principal.id, principalName(row.principal))
    }
    for (const [key, name] of counts.names) push(key, name)
    push(CREATOR_UNATTRIBUTED, i18nT('sharedCrew.unattributed'))
    return out
  }, [shown, counts, people, viewerId, selectedKeys])
  /** Selected keys narrowed to rows the section lists. A stored person who is no
   *  longer listed matches nothing and has no row left to untick, so the key is
   *  ignored, not pruned: a People read still loading must not drop a choice. */
  const activeCreatorKeys = useMemo(() => {
    const listed = new Set(creatorFilterRows.map(row => row.key))
    return new Set([...selectedKeys].filter(key => listed.has(key)))
  }, [creatorFilterRows, selectedKeys])
  /** Ticked rows OR together, as the model rows do. */
  const creatorFilterPasses = useMemo(
    () => (slot: Slot) => {
      if (activeCreatorKeys.size === 0) return true
      const key = slotCreatorKey(slot.attribution, viewerId)
      return key !== null && activeCreatorKeys.has(key)
    },
    [activeCreatorKeys, viewerId],
  )
  const activeCreatorRows = useMemo(
    () => creatorFilterRows.filter(row => activeCreatorKeys.has(row.key)),
    [creatorFilterRows, activeCreatorKeys],
  )
  return { creatorFilterRows, activeCreatorKeys, activeCreatorRows, creatorFilterPasses }
}
