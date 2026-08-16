import { useMemo } from 'react'
import { ArrowUp, ArrowDown, ArrowUpDown, ListFilter, X } from 'lucide-react'
import { useIssueRadar } from '../context'
import { PR_SORT_FIELDS } from '../lib/format'
import FilterRow from './FilterRow'
import LabelPalette from './LabelPalette'
import { providerTerms } from '../lib/links'

import { i18nT } from '../../../i18n/t'
/** Body of the "Pull requests" accordion section: Sort options, the state
 * (open / merged / closed) + draft / mine / review toggles, and the label
 * palette — the PR analogue of FiltersSection. Reads and drives everything
 * through the shared context (the PR-scoped slice). */
export default function PrFiltersSection() {
  const {
    prSortKey, prSortDir, cyclePrSort,
    prStateFilter, setPrStateFilter, setSelectedPull, openPulls,
    prDraftOnly, togglePrDraftOnly,
    prAuthoredByMe, togglePrAuthoredByMe,
    prAssignedToMe, togglePrAssignedToMe,
    prReviewRequestedByMe, togglePrReviewRequestedByMe,
    prCreatedByMember, togglePrCreatedByMember, hasMemberPulls,
    me, anyPrFilterActive, clearPrFilters,
    repoLabels, countByPrLabel, prSelectedLabels, togglePrLabel,
    labelsLoading, labelsError,
    active,
  } = useIssueRadar()
  // Provider vocabulary: GitLab calls these merge requests, and calling them
  // pull requests in a GitLab workspace is simply wrong copy.
  const terms = providerTerms(active)

  // Labels ordered by their PR-usage count (most-used first) — the PR analogue
  // of the issue-scoped sortedRepoLabels.
  const sortedPrLabels = useMemo(
    () => [...repoLabels].sort((a, b) => (countByPrLabel.get(b.name) ?? 0) - (countByPrLabel.get(a.name) ?? 0)),
    [repoLabels, countByPrLabel],
  )

  const setState = (s: 'open' | 'closed' | 'merged') => {
    setPrStateFilter(s); setSelectedPull(null); openPulls()
  }

  return (
    <>
      <div className="px-3 pt-2">
        <div className="flex items-center gap-1.5 mb-1.5 text-[12px] font-semibold text-muted uppercase tracking-[.05em]">
          <ArrowUpDown size={12} /> {i18nT('apps.issueRadar.components.prFiltersSection.sort')}
        </div>
        <div className="flex flex-col gap-0.5">
          {PR_SORT_FIELDS.map((f) => {
            const isActive = f.key === prSortKey
            const DirIcon = prSortDir === 'asc' ? ArrowUp : ArrowDown
            return (
              <button
                key={f.key}
                onClick={() => cyclePrSort(f.key)}
                className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-[13px] text-left transition-colors ${
                  isActive
                    ? 'bg-accent-subtle text-text font-medium cursor-pointer'
                    : 'text-muted hover:bg-bg-hover cursor-pointer'
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
            <ListFilter size={12} /> {i18nT('apps.issueRadar.components.prFiltersSection.filters')}
          </span>
          {anyPrFilterActive && (
            <button
              onClick={clearPrFilters}
              className="ml-auto inline-flex items-center gap-0.5 text-[12px] text-muted hover:text-text cursor-pointer bg-transparent"
            >
              <X size={11} /> {i18nT('apps.issueRadar.components.prFiltersSection.clear')}
            </button>
          )}
        </div>
        <div className="flex flex-col gap-0.5">
          {/* Above the rule: the lifecycle — mutually exclusive (open / merged /
              closed replace one another). Below it: independent toggles that
              combine. The rule makes that difference visible. */}
          <FilterRow label={i18nT('apps.issueRadar.components.prFiltersSection.open')} active={prStateFilter === 'open'} onToggle={() => setState('open')} />
          <FilterRow label={i18nT('apps.issueRadar.components.prFiltersSection.merged')} active={prStateFilter === 'merged'} onToggle={() => setState('merged')} />
          <FilterRow label={i18nT('apps.issueRadar.components.prFiltersSection.closed_unmerged')} active={prStateFilter === 'closed'} onToggle={() => setState('closed')} />
          <div className="my-1 border-t border-border" role="separator" />
          <FilterRow label={i18nT('apps.issueRadar.components.prFiltersSection.draft')} active={prDraftOnly} onToggle={togglePrDraftOnly} />
          {/* The three "me" filters are answered by a repo-wide GitHub SEARCH
              rather than by filtering the capped list, so they find your older
              PRs too (see context: prPersonFilterActive). */}
          <FilterRow label={i18nT('apps.issueRadar.components.prFiltersSection.authored_by_me')} active={prAuthoredByMe} disabled={!me} onToggle={togglePrAuthoredByMe} />
          <FilterRow label={i18nT('apps.issueRadar.components.prFiltersSection.assigned_to_me')} active={prAssignedToMe} disabled={!me} onToggle={togglePrAssignedToMe} />
          <FilterRow
            label={i18nT('apps.issueRadar.components.prFiltersSection.review_requested')}
            active={prReviewRequestedByMe}
            disabled={!me}
            disabledHint={i18nT('apps.issueRadar.components.prFiltersSection.sign_in_to_gh_to_filter_by_your_review_requests')}
            onToggle={togglePrReviewRequestedByMe}
          />
          {/* Unlike the three above, this one is answered client-side: the row's
              author / association is already on every row (search rows included),
              so it needs no extra query. */}
          <FilterRow
            label={i18nT('apps.issueRadar.components.prFiltersSection.created_by_member')}
            active={prCreatedByMember}
            disabled={!hasMemberPulls}
            disabledHint={i18nT('apps.issueRadar.components.prFiltersSection.no_repo_members_found_among_these', { label: terms.changeRequestPlural })}
            onToggle={togglePrCreatedByMember}
          />
        </div>
      </div>

      <LabelPalette
        labels={sortedPrLabels}
        countByLabel={countByPrLabel}
        selected={prSelectedLabels}
        onToggle={togglePrLabel}
        loading={labelsLoading}
        error={labelsError}
      />
    </>
  )
}
