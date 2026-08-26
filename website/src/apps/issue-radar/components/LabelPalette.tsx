import { useMemo, useState } from 'react'
import { Search, Tag, X } from 'lucide-react'
import type { RepoLabel } from '../api'
import LabelRow from './LabelRow'

import { i18nT } from '../../../i18n/t'
/** The "Labels" block of a rail filter section: a header with a toggleable
 * search box and the label palette itself. Shared by the issue FiltersSection
 * and the pull-request PrFiltersSection — the two differ only in WHICH counts and
 * selection they pass in, so the markup lives here once.
 *
 * The label search is UI-only local state: it narrows which pills are shown
 * without touching the caller's filter state (selecting a pill still filters).
 * Non-matching rows stay mounted and animate closed rather than unmounting, so
 * the palette morphs instead of jumping. */
export default function LabelPalette({
  labels, countByLabel, selected, onToggle, loading, error,
}: {
  /** Repo labels, already ordered by the caller (usually most-used first). */
  labels: RepoLabel[]
  countByLabel: Map<string, number>
  selected: Set<string>
  onToggle: (name: string) => void
  loading: boolean
  error: Error | null
}) {
  const [labelQuery, setLabelQuery] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  const toggleSearch = () => {
    // Opening reveals the box; closing hides it AND clears the query so the
    // collapsed state always means "no active label search".
    if (searchOpen) { setSearchOpen(false); setLabelQuery('') }
    else setSearchOpen(true)
  }

  const matchedNames = useMemo(() => {
    const q = labelQuery.trim().toLowerCase()
    const names = new Set<string>()
    for (const l of labels) {
      if (!q || l.name.toLowerCase().includes(q) || (l.description ?? '').toLowerCase().includes(q)) {
        names.add(l.name)
      }
    }
    return names
  }, [labels, labelQuery])

  return (
    <div className="px-3 pt-5">
      <div className="pb-2 flex items-center">
        <span className="inline-flex items-center gap-1.5 text-[12px] font-semibold text-muted uppercase tracking-[.05em]">
          <Tag size={12} /> {i18nT('apps.issueRadar.components.labelPalette.labels')}
        </span>
        {labels.length > 0 && (
          <button
            onClick={toggleSearch}
            aria-label={searchOpen ? i18nT('apps.issueRadar.components.labelPalette.close_label_search') : i18nT('apps.issueRadar.components.labelPalette.search_labels')}
            aria-expanded={searchOpen}
            title={i18nT('apps.issueRadar.components.labelPalette.search_labels')}
            className={`ml-auto p-0.5 rounded cursor-pointer bg-transparent transition-colors ${
              searchOpen ? 'text-accent' : 'text-muted hover:text-text hover:bg-bg-hover'
            }`}
          >
            <Search size={13} />
          </button>
        )}
      </div>
      {loading && <div className="text-[12px] text-muted">{i18nT('apps.issueRadar.components.labelPalette.loading_labels')}</div>}
      {error && <div className="text-[12px] text-danger">{error.message}</div>}
      {labels.length === 0 && !loading && (
        <div className="text-[12px] text-muted">{i18nT('apps.issueRadar.components.labelPalette.no_labels_on_this_repo')}</div>
      )}
      {labels.length > 0 && searchOpen && (
        <div className="relative mb-2">
          <input
            value={labelQuery}
            onChange={(e) => setLabelQuery(e.target.value)}
            autoFocus
            aria-label={i18nT('apps.issueRadar.components.labelPalette.search_labels')}
            placeholder={i18nT('apps.issueRadar.components.labelPalette.search_labels_2')}
            className="w-full box-border text-[13px] pl-3 pr-7 py-1.5 rounded-md bg-bg text-text border border-border placeholder:text-muted"
          />
          <button
            onClick={() => { setLabelQuery(''); setSearchOpen(false) }}
            aria-label={i18nT('apps.issueRadar.components.labelPalette.close_label_search')}
            className="absolute right-1.5 top-1/2 -translate-y-1/2 p-0.5 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent"
          >
            <X size={13} />
          </button>
        </div>
      )}
      <div className="flex flex-col items-start">
        {labels.map((label: RepoLabel) => {
          const shown = matchedNames.has(label.name)
          return (
            <div
              key={label.name}
              aria-hidden={!shown}
              className={`grid w-full max-w-full transition-all duration-200 ease-out ${
                shown
                  ? 'grid-rows-[1fr] opacity-100 mt-1.5 first:mt-0'
                  : 'grid-rows-[0fr] opacity-0 mt-0 pointer-events-none'
              }`}
            >
              <div className="overflow-hidden min-h-0">
                <LabelRow
                  name={label.name}
                  color={label.color}
                  count={countByLabel.get(label.name) ?? 0}
                  selected={selected.has(label.name)}
                  title={label.description || label.name}
                  onClick={() => onToggle(label.name)}
                />
              </div>
            </div>
          )
        })}
      </div>
      {searchOpen && matchedNames.size === 0 && (
        <div className="text-[12px] text-muted">{i18nT('apps.issueRadar.components.labelPalette.no_labels_match_query', { query: labelQuery })}</div>
      )}
    </div>
  )
}
