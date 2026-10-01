/** The chat tag vocabulary and the lookups the rows and the tag filter read. */
import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import type { ChatTag } from '../../types'
import { api } from '../../api/client'
import type { Slot } from './types'

/** Stable empty fallback for the chat-tags query. Referenced instead of a
 *  `= []` destructuring default so `tagById` (a memoized SessionRow prop)
 *  keeps one identity while the query has no data. */
const NO_TAGS: ChatTag[] = []

/** The tag vocabulary query and the lookups built from it. */
export function useSidebarTags({ filterTagIds, localSlots }: {
  filterTagIds: Set<string>
  localSlots: Slot[]
}) {
  // Tags via React Query (dynamic vocabulary, defaults seeded server-side).
  // `tagsData` stays undefined until the query resolves — FolderConfigModal
  // needs that distinction (unknown vocabulary must not be treated as empty,
  // or a modal opened mid-load would seed a partial tag list and a save would
  // silently delete the folder's existing tags). The resolved `tags` fallback
  // is the module-level NO_TAGS constant, NOT a `?? []` literal: a fresh array
  // minted on every render while the query has no data would rebuild `tagById`
  // each time and hand every memoized SessionRow a changed prop — silently
  // voiding the row memo boundary the render-probe test pins.
  const { data: tagsData, isError: tagsQueryFailed, refetch: refetchTags } = useQuery<ChatTag[]>({ queryKey: ['chat-tags'], queryFn: () => api.chatTags() })
  const tags = tagsData ?? NO_TAGS
  // A FAILED chat-tags query never self-heals (staleTime Infinity, freshness
  // is WS-driven), so recovery is the user-driven inline Retry in the folder
  // modal's error line (`onRetryTags` → `refetchTags`). No automatic retry
  // fires on modal open — a background attempt against a down endpoint only
  // duplicates the button and needs its own loop guard to exist safely.
  const tagById = useMemo(() => {
    const m: Record<string, ChatTag> = {}
    for (const t of tags) m[t.id] = t
    return m
  }, [tags])
  /** Selected ids narrowed to tags that STILL EXIST. Deleting a tag leaves its id
   *  in localStorage, and an unresolvable id matches no session — so without this
   *  guard, deleting the last selected tag would hide every session with no
   *  control left on screen to explain why. Unresolvable ids are ignored rather
   *  than pruned: the tag list is a server query, so an id absent from a slow or
   *  failed fetch must not silently destroy a valid selection. */
  const activeTagIds = useMemo(
    () => new Set([...filterTagIds].filter(id => tagById[id])),
    [filterTagIds, tagById],
  )
  /** Rows for the filter menu's Tags section, in the tag vocabulary's own order.
   *  Counts come from all `slots`, NOT `filteredSlots`, so they describe the
   *  vocabulary rather than the current selection — otherwise every unselected tag
   *  would read 0 the moment any tag was selected, which is the number a user
   *  consults precisely when deciding what to select next. */
  const tagFilterRows = useMemo(
    () => [...tags]
      .sort((a, b) => a.order - b.order)
      .map(t => ({
        tag: t,
        count: localSlots.filter(s => (s.tags ?? []).includes(t.id)).length,
        selected: filterTagIds.has(t.id),
      })),
    [tags, localSlots, filterTagIds],
  )
  /** Names of the selected tags, in vocabulary order. Disjunction, not a comma
   *  join: selection is a union, so a screen reader should hear "Blocked or
   *  Idea", and `fmtList` is what makes that read correctly in every language. */
  const activeTagNames = useMemo(
    () => tagFilterRows.filter(({ tag: t }) => activeTagIds.has(t.id)).map(({ tag: t }) => t.name),
    [tagFilterRows, activeTagIds],
  )
  return { tagsData, tagsQueryFailed, refetchTags, tagById, activeTagIds, tagFilterRows, activeTagNames }
}
