// Centered empty state for the issue / PR list columns.
//
// Replaces a top-left one-line "No matching pull requests.", which read as a
// glitch — a lone sentence pinned under the search box, with the rest of the
// column blank. Centering it in the column and pairing it with an icon makes the
// emptiness look deliberate, and the two icons distinguish the two causes:
// a search that matched nothing vs. filters that exclude everything.
import { SearchX, FilterX } from 'lucide-react'

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
        {searching ? `No ${label} match your search.` : `No matching ${label}.`}
      </div>
      {!searching && (
        <div className="text-[11.5px] text-muted opacity-70">
          Try clearing a filter in the sidebar.
        </div>
      )}
    </div>
  )
}
