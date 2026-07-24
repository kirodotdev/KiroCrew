import { useMemo, useState } from 'react'
import { ArrowUp, ArrowDown, ArrowUpDown, ListFilter, Search, Tag, X } from 'lucide-react'
import { useIssueRadar } from '../context'
import { SORT_FIELDS } from '../lib/format'
import type { RepoLabel } from '../api'
import FilterRow from './FilterRow'
import LabelRow from './LabelRow'

/** Body of the "Filters" accordion section: Sort options, the state / mine
 * toggles, and the label palette. Reads and drives everything through the
 * shared context. */
export default function FiltersSection() {
  const {
    sortKey, sortDir, cycleSort,
    stateFilter, setStateFilter, setSelectedIssue, openIssues,
    requestedByMe, toggleRequestedByMe, assignedToMe, toggleAssignedToMe,
    createdByMember, toggleCreatedByMember, hasMemberIssues,
    me, anyFilterActive, clearFilters,
    sortedRepoLabels, countByLabel, selectedLabels, toggleLabel,
    labelsLoading, labelsError, repoLabels,
  } = useIssueRadar()

  // Local, UI-only label search — narrows which label pills are shown without
  // touching the shared filter state (selecting a pill still filters issues).
  const [labelQuery, setLabelQuery] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  const toggleSearch = () => {
    // Opening reveals the box; closing hides it AND clears the query so the
    // collapsed state always means "no active label search".
    if (searchOpen) { setSearchOpen(false); setLabelQuery('') }
    else setSearchOpen(true)
  }
  // Label search is just another filter over the palette, so instead of
  // dropping non-matches from the list we keep every pill mounted and animate
  // it in/out — matchedNames drives which rows are expanded vs collapsed.
  const matchedNames = useMemo(() => {
    const q = labelQuery.trim().toLowerCase()
    const names = new Set<string>()
    for (const l of sortedRepoLabels) {
      if (!q || l.name.toLowerCase().includes(q) || (l.description ?? '').toLowerCase().includes(q)) {
        names.add(l.name)
      }
    }
    return names
  }, [sortedRepoLabels, labelQuery])

  return (
    <>
      <div className="px-3 pt-2">
        <div className="flex items-center gap-1.5 mb-1.5 text-[12px] font-semibold text-muted uppercase tracking-[.05em]">
          <ArrowUpDown size={12} /> Sort
        </div>
        <div className="flex flex-col gap-0.5">
          {SORT_FIELDS.map((f) => {
            const isActive = f.key === sortKey && !f.soon
            const DirIcon = sortDir === 'asc' ? ArrowUp : ArrowDown
            return (
              <button
                key={f.key}
                disabled={f.soon}
                title={f.soon ? 'AI-ranked order — coming soon' : undefined}
                onClick={() => { if (!f.soon) cycleSort(f.key) }}
                className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-[13px] text-left transition-colors ${
                  f.soon
                    ? 'text-muted opacity-60 cursor-default'
                    : isActive
                      ? 'bg-accent-subtle text-text font-medium cursor-pointer'
                      : 'text-muted hover:bg-bg-hover cursor-pointer'
                }`}
              >
                <f.icon size={14} className="flex-shrink-0" />
                <span className="flex-1">{f.label}</span>
                {f.soon && (
                  <span className="text-[10px] uppercase tracking-wide rounded px-1 py-0.5 bg-bg-hover text-muted whitespace-nowrap">coming soon</span>
                )}
                {isActive && <DirIcon size={14} className="text-accent" />}
              </button>
            )
          })}
        </div>
      </div>

      <div className="px-3 pt-5">
        <div className="flex items-center mb-1.5">
          <span className="inline-flex items-center gap-1.5 text-[12px] font-semibold text-muted uppercase tracking-[.05em]">
            <ListFilter size={12} /> Filters
          </span>
          {anyFilterActive && (
            <button
              onClick={clearFilters}
              className="ml-auto inline-flex items-center gap-0.5 text-[12px] text-muted hover:text-text cursor-pointer bg-transparent"
            >
              <X size={11} /> clear
            </button>
          )}
        </div>
        <div className="flex flex-col gap-0.5">
          <FilterRow label="Open" active={stateFilter === 'open'} onToggle={() => { setStateFilter('open'); setSelectedIssue(null); openIssues() }} />
          <FilterRow label="Closed" active={stateFilter === 'closed'} onToggle={() => { setStateFilter('closed'); setSelectedIssue(null); openIssues() }} />
          <FilterRow label="Requested by me" active={requestedByMe} disabled={!me} onToggle={toggleRequestedByMe} />
          <FilterRow label="Assigned to me" active={assignedToMe} disabled={!me} onToggle={toggleAssignedToMe} />
          <FilterRow
            label="Created by member"
            active={createdByMember}
            disabled={!hasMemberIssues}
            disabledHint="No repo members found among these issues"
            onToggle={toggleCreatedByMember}
          />
        </div>
      </div>

      <div className="px-3 pt-5">
        <div className="pb-2 flex items-center">
          <span className="inline-flex items-center gap-1.5 text-[12px] font-semibold text-muted uppercase tracking-[.05em]">
            <Tag size={12} /> Labels
          </span>
          {repoLabels.length > 0 && (
            <button
              onClick={toggleSearch}
              aria-label={searchOpen ? 'Close label search' : 'Search labels'}
              aria-expanded={searchOpen}
              title="Search labels"
              className={`ml-auto p-0.5 rounded cursor-pointer bg-transparent transition-colors ${
                searchOpen ? 'text-accent' : 'text-muted hover:text-text hover:bg-bg-hover'
              }`}
            >
              <Search size={13} />
            </button>
          )}
        </div>
        {labelsLoading && <div className="text-[12px] text-muted">Loading labels…</div>}
        {labelsError && <div className="text-[12px] text-danger">{labelsError.message}</div>}
        {repoLabels.length === 0 && !labelsLoading && (
          <div className="text-[12px] text-muted">No labels on this repo.</div>
        )}
        {repoLabels.length > 0 && searchOpen && (
          <div className="relative mb-2">
            <input
              value={labelQuery}
              onChange={(e) => setLabelQuery(e.target.value)}
              autoFocus
              aria-label="Search labels"
              placeholder="Search labels…"
              className="w-full box-border text-[13px] pl-3 pr-7 py-1.5 rounded-md bg-bg text-text border border-border placeholder:text-muted"
            />
            <button
              onClick={() => { setLabelQuery(''); setSearchOpen(false) }}
              aria-label="Close label search"
              className="absolute right-1.5 top-1/2 -translate-y-1/2 p-0.5 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent"
            >
              <X size={13} />
            </button>
          </div>
        )}
        <div className="flex flex-col items-start">
          {sortedRepoLabels.map((label: RepoLabel) => {
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
                    selected={selectedLabels.has(label.name)}
                    title={label.description || label.name}
                    onClick={() => toggleLabel(label.name)}
                  />
                </div>
              </div>
            )
          })}
        </div>
        {searchOpen && matchedNames.size === 0 && (
          <div className="text-[12px] text-muted">No labels match “{labelQuery}”.</div>
        )}
      </div>
    </>
  )
}
