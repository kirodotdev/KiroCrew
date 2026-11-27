/** The "Sort & filter" menu's "Started by" section and its chip under the
 *  search box, on a Shared Crew. Both render nothing while the section is hidden
 *  (`useSharedCrewListView().creatorFilterShown`), since then it has no rows. */
import { Check } from 'lucide-react'
import { DropdownMenuItem, DropdownMenuSeparator } from '../../components/ui/dropdown-menu'
import { FilterChip, FilterMenuLabel } from '../../components/SearchFilterBar'
import { i18nT } from '../../i18n/t'
import { fmtList, fmtNumber } from '../../i18n/format'
import type { CreatorFilterRow } from './creators'
import type { CreatorCounts } from './rows'

export interface CreatorFilterMenuView {
  rows: CreatorFilterRow[]
  activeRows: CreatorFilterRow[]
  counts: CreatorCounts
  toggle: (key: string) => void
  clear: () => void
}

/** The section: a checkbox row per creator with its session count, like the
 *  model rows. Ticked rows OR together. */
export function CreatorFilterSection({ view }: { view: CreatorFilterMenuView }) {
  if (view.rows.length === 0) return null
  return (
    <>
      <DropdownMenuSeparator />
      <FilterMenuLabel>{i18nT('sharedCrew.started_by_heading')}</FilterMenuLabel>
      {view.rows.map(row => (
        <DropdownMenuItem
          key={row.key}
          title={row.selected
            ? i18nT('sharedCrew.stop_filtering_by_creator', { name: row.label })
            : i18nT('sharedCrew.show_only_started_by', { name: row.label })}
          // Keep the menu open so several creators can be ticked.
          onSelect={e => { e.preventDefault(); view.toggle(row.key) }}
          data-testid={`creator-filter-row-${row.key}`}
          role="menuitemcheckbox"
          aria-checked={row.selected}
        >
          <span
            aria-hidden="true"
            className={`w-3.5 h-3.5 shrink-0 rounded-[3px] border flex items-center justify-center ${row.selected ? 'border-accent bg-accent text-accent-fg' : 'border-border bg-transparent'}`}
          >
            {row.selected && <Check size={10} strokeWidth={3} />}
          </span>
          {/* A long name or e-mail is cut with an ellipsis and read in full on hover. */}
          <span className="flex-1 min-w-0 truncate" title={row.label} data-testid={`creator-filter-name-${row.key}`}>{row.label}</span>
          {/* 0 is rendered, not omitted: a zero-count row is exactly the one
              that blanks the list when ticked. */}
          <span className="text-muted text-[11px] shrink-0">{row.count}</span>
        </DropdownMenuItem>
      ))}
    </>
  )
}

/** The chip, in its own row like the model chip, with the line saying how many
 *  sessions the filter shows and how many it leaves out. Its × clears it. */
export function CreatorFilterChip({ view }: { view: CreatorFilterMenuView }) {
  if (view.activeRows.length === 0) return null
  const names = fmtList(view.activeRows.map(row => row.label), { type: 'disjunction' })
  const label = i18nT('sharedCrew.started_by_chip', { names })
  return (
    <div className="px-3 pb-1 min-w-0">
      <FilterChip
        aggregate
        testId="creator-filter-chip"
        label={label}
        clearLabel={i18nT('pages.chatSidebar.clear_named_filter', { filter: label })}
        onClear={view.clear}
      />
      <p className="text-muted text-[11px] pt-0.5" role="status" data-testid="creator-filter-counts">
        {i18nT('sharedCrew.filter_counts', { shown: fmtNumber(view.counts.matching), hidden: fmtNumber(view.counts.hidden) })}
      </p>
    </div>
  )
}
