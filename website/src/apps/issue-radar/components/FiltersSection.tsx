import { ArrowUp, ArrowDown, ArrowUpDown, ListFilter, X } from 'lucide-react'
import { useIssueRadar } from '../context'
import { SORT_FIELDS } from '../lib/format'
import FilterRow from './FilterRow'
import LabelPalette from './LabelPalette'

import { i18nT } from '../../../i18n/t'
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
    labelsLoading, labelsError,
  } = useIssueRadar()

  return (
    <>
      <div className="px-3 pt-2">
        <div className="flex items-center gap-1.5 mb-1.5 text-[12px] font-semibold text-muted uppercase tracking-[.05em]">
          <ArrowUpDown size={12} /> {i18nT('apps.issueRadar.components.filtersSection.sort')}
        </div>
        <div className="flex flex-col gap-0.5">
          {SORT_FIELDS.map((f) => {
            const isActive = f.key === sortKey
            const DirIcon = sortDir === 'asc' ? ArrowUp : ArrowDown
            return (
              <button
                key={f.key}
                onClick={() => cycleSort(f.key)}
                className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-[13px] text-left transition-colors cursor-pointer ${
                  isActive
                    ? 'bg-accent-subtle text-text font-medium'
                    : 'text-muted hover:bg-bg-hover'
                }`}
              >
                <f.icon size={14} className="flex-shrink-0" />
                <span className="flex-1">{f.label}</span>
                {isActive && <DirIcon size={14} className="text-accent" />}
              </button>
            )
          })}
        </div>
      </div>

      <div className="px-3 pt-5">
        <div className="flex items-center mb-1.5">
          <span className="inline-flex items-center gap-1.5 text-[12px] font-semibold text-muted uppercase tracking-[.05em]">
            <ListFilter size={12} /> {i18nT('apps.issueRadar.components.filtersSection.filters')}
          </span>
          {anyFilterActive && (
            <button
              onClick={clearFilters}
              className="ml-auto inline-flex items-center gap-0.5 text-[12px] text-muted hover:text-text cursor-pointer bg-transparent"
            >
              <X size={11} /> {i18nT('apps.issueRadar.components.filtersSection.clear')}
            </button>
          )}
        </div>
        <div className="flex flex-col gap-0.5">
          {/* Above the rule: the STATE — mutually exclusive, picking one
              replaces the other. Below it: independent toggles that combine.
              The rule makes that difference visible instead of leaving the user
              to discover it by clicking. */}
          <FilterRow label={i18nT('apps.issueRadar.components.filtersSection.open')} active={stateFilter === 'open'} onToggle={() => { setStateFilter('open'); setSelectedIssue(null); openIssues() }} />
          <FilterRow label={i18nT('apps.issueRadar.components.filtersSection.closed')} active={stateFilter === 'closed'} onToggle={() => { setStateFilter('closed'); setSelectedIssue(null); openIssues() }} />
          <div className="my-1 border-t border-border" role="separator" />
          <FilterRow label={i18nT('apps.issueRadar.components.filtersSection.requested_by_me')} active={requestedByMe} disabled={!me} onToggle={toggleRequestedByMe} />
          <FilterRow label={i18nT('apps.issueRadar.components.filtersSection.assigned_to_me')} active={assignedToMe} disabled={!me} onToggle={toggleAssignedToMe} />
          <FilterRow
            label={i18nT('apps.issueRadar.components.filtersSection.created_by_member')}
            active={createdByMember}
            disabled={!hasMemberIssues}
            disabledHint="No repo members found among these issues"
            onToggle={toggleCreatedByMember}
          />
        </div>
      </div>

      <LabelPalette
        labels={sortedRepoLabels}
        countByLabel={countByLabel}
        selected={selectedLabels}
        onToggle={toggleLabel}
        loading={labelsLoading}
        error={labelsError}
      />
    </>
  )
}
