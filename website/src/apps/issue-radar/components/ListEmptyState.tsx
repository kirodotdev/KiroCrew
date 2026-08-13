// Centered empty state for the issue / PR list columns.
//
// Centered in the column and paired with an icon so the emptiness looks
// deliberate rather than like a glitch, and the two icons distinguish the two
// causes: a search that matched nothing vs. filters that exclude everything.
import { SearchX, FilterX } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
export default function ListEmptyState({
  searching, label,
}: {
  /** True when a search query is active — the query, not the filters, is why
   * the list is empty, so the copy and icon say so. */
  searching: boolean
  /** Plural noun for the items, already capitalized ("Issues" / "Pull Requests"). */
  label: string
}) {
  const Icon = searching ? SearchX : FilterX
  return (
    // Fills the column so the block lands in the optical centre rather than
    // hugging the search box.
    <div className="flex-1 min-h-0 flex flex-col items-center justify-center gap-2.5 text-center px-6">
      <Icon size={26} className="text-muted opacity-50" strokeWidth={1.5} />
      <div className="text-[13px] text-muted">
        {searching
          ? i18nT('apps.issueRadar.components.listEmptyState.no_match_your_search', { label })
          : i18nT('apps.issueRadar.components.listEmptyState.no_matching', { label })}
      </div>
      {!searching && (
        <div className="text-[11.5px] text-muted opacity-70">
          {i18nT('apps.issueRadar.components.listEmptyState.try_clearing_a_filter_in_the_sidebar')}
        </div>
      )}
    </div>
  )
}
