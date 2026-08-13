// The rail's tabbed lists: the selected repo's pull requests, and your reviews.
//
// Pull requests are always the active repo's — that list only exists in relation
// to a repo. Reviews default to the same scope (which is what you want while
// working a repo) but can be widened to every review, because a review outlives
// the repo you happened to have in focus when you started it: with this the only
// review list in the app, scoping alone would strand finished work.
//
// PRs lead, that being the path you take to start work, and Reviews carries a
// live count so an in-flight review is never hidden behind a tab you are not
// looking at.
import { useState } from 'react'
import { GitPullRequest, ListChecks } from 'lucide-react'

import { repoSlug, useSage } from '../context'
import PrPickList from './PrPickList'
import RunList from './RunList'

import { i18nT } from '../../../i18n/t'
function Tab({
  label, icon: Icon, active, badge, onClick,
}: {
  label: string
  icon: typeof GitPullRequest
  active: boolean
  badge?: number
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      role="tab"
      aria-selected={active}
      // Same ring as every other control; without it the browser draws its own
      // blue outline, which is what made a focused tab look mis-styled.
      className={`flex-1 inline-flex items-center justify-center gap-1.5 px-2 py-1.5 text-[12px] font-medium rounded-md transition-colors cursor-pointer focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 ${
        active
          ? 'bg-bg-elevated text-text border border-border'
          : 'bg-transparent text-muted border border-transparent hover:text-text'
      }`}
    >
      <Icon size={13} aria-hidden="true" />
      {label}
      {badge ? (
        <span className="inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-accent-subtle text-accent">
          <span
            aria-hidden="true"
            className="w-1.5 h-1.5 rounded-full bg-accent animate-pulse motion-reduce:animate-none"
          />
          {badge}
        </span>
      ) : null}
    </button>
  )
}

export default function MiddleColumn() {
  const {
    runs, repoRuns, runsLoading, runsError, selectedRunId, selectRun,
    listTab, setListTab, activeRepo, setMainView, deleteRun, deleting,
  } = useSage()
  const slug = activeRepo ? repoSlug(activeRepo) : null

  // Scoped by default while a repo is in focus; widened on request. Reset when
  // the repo changes, so the choice never silently carries over to another repo.
  const [allRepos, setAllRepos] = useState(false)
  const [lastSlug, setLastSlug] = useState(slug)
  if (slug !== lastSlug) {
    setLastSlug(slug)
    if (allRepos) setAllRepos(false)
  }
  const scoped = !!activeRepo && !allRepos
  const shown = scoped ? repoRuns : runs
  const hiddenByScope = scoped ? runs.length - repoRuns.length : 0
  // Counts every live run, scoped or not: a review running elsewhere still wants
  // your attention, and hiding that behind the scope would be a worse lie than a
  // badge that occasionally exceeds the visible rows.
  const live = runs.filter((r) => r.status === 'running').length

  // The rail is visible from Learning and Settings too, so opening a review has
  // to bring the review surface back with it.
  const open = (runId: string) => {
    setMainView('reviews')
    selectRun(runId)
  }

  return (
    <div className="flex flex-col min-h-0 h-full">
      <div
        role="tablist"
        aria-label={slug
    ? i18nT('apps.codeReviewSage.components.middleColumn.repo_lists_label', { repo: slug })
    : i18nT('apps.codeReviewSage.components.middleColumn.lists_label')}
        className="flex items-center gap-1 px-2 pt-2 flex-shrink-0"
      >
        <Tab
          label={i18nT('apps.codeReviewSage.components.middleColumn.pull_requests')}
          icon={GitPullRequest}
          active={listTab === 'pulls'}
          onClick={() => setListTab('pulls')}
        />
        <Tab
          label={i18nT('apps.codeReviewSage.components.middleColumn.reviews')}
          icon={ListChecks}
          active={listTab === 'reviews'}
          badge={live}
          onClick={() => setListTab('reviews')}
        />
      </div>

      <div className="flex-1 min-h-0">
        {listTab === 'pulls' ? (
          <PrPickList />
        ) : (
          <div className="flex flex-col min-h-0 h-full">
            {/* Only shown when it changes something: with nothing hidden, a scope
                control is noise. */}
            {(hiddenByScope > 0 || allRepos) && (
              <div className="px-3 pt-2 flex items-center gap-1.5 text-[11px] text-muted flex-shrink-0">
                <span className="truncate">
                  {scoped
          ? i18nT('apps.codeReviewSage.components.middleColumn.this_repo_only', { repo: slug })
          : i18nT('apps.codeReviewSage.components.middleColumn.all_repos')}
                </span>
                <button
                  type="button"
                  onClick={() => setAllRepos((v) => !v)}
                  className="ml-auto flex-shrink-0 bg-transparent text-accent hover:underline cursor-pointer p-0"
                >
                  {scoped
            ? i18nT('apps.codeReviewSage.components.middleColumn.show_all_count', { count: runs.length })
            : i18nT('apps.codeReviewSage.components.middleColumn.just_this_repo', { repo: slug })}
                </button>
              </div>
            )}
            <div className="flex-1 min-h-0">
              <RunList
                runs={shown}
                loading={runsLoading}
                error={runsError?.message ?? null}
                selectedRunId={selectedRunId}
                onSelect={open}
                onDelete={deleteRun}
                deleting={deleting}
                onNewReview={() => setListTab('pulls')}
                emptyTitle={scoped
              ? i18nT('apps.codeReviewSage.components.middleColumn.no_reviews_for_repo', { repo: slug })
              : i18nT('apps.codeReviewSage.components.middleColumn.no_reviews_yet')}
                emptyHint={scoped
                  ? i18nT('apps.codeReviewSage.components.middleColumn.repo_reviews_appear_here')
                  : i18nT('apps.codeReviewSage.components.middleColumn.pick_a_repo_then_a_pr')}
              />
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
