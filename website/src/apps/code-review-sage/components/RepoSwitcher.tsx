// The repo picker, pinned to the TOP of the rail as a dropdown.
//
// Was a stacked list panel, which spent a fixed slice of the rail on a choice you
// make once and then leave alone — the lists it scopes are what you actually
// scan. Collapsed to a single row, it reads as what it is: the filter everything
// below it answers to.
//
// Mechanics copied from Issue Radar's RepoSwitcher (shared Radix DropdownMenu,
// never a native <select>, per product decision) so the two builtins behave
// identically. Sage adds what its own flow needs: an unset state (Sage can sit
// with no repo selected, Issue Radar always has one), a filter box for long
// pinned lists, and an add-a-repo action. Removal lives in the Add-repos modal:
// an interactive control nested inside a menu item is invalid a11y, and Radix
// closes the menu before the inner click lands.
import { useMemo, useState } from 'react'
import { Check, ChevronDown, Clock, FolderGit2, Plus } from 'lucide-react'

import GithubLogo from '../../../components/icons/GithubLogo'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from '../../../components/ui/dropdown-menu'
import { repoSlug, useSage, type ActiveRepo } from '../context'
import { loadRecentRepos, rememberRecentRepo } from '../lib/persist'

import { i18nT } from '../../../i18n/t'
export default function RepoSwitcher() {
  const {
    pinnedRepos, pinnedLoading, activeRepo, setActiveRepo,
    openAddRepos, pinError, setListTab, setMainView,
  } = useSage()
  const [query, setQuery] = useState('')
  // Most-recently-picked, newest first. The pinned list is ordered by when each
  // repo was ADDED, which says nothing about the two or three you keep coming
  // back to, so those get their own section at the top.
  const [recent, setRecent] = useState(loadRecentRepos)

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return pinnedRepos
    return pinnedRepos.filter((r) => `${r.owner}/${r.repo}`.toLowerCase().includes(q))
  }, [pinnedRepos, query])

  // Only recents that are still pinned, so a removed repo cannot linger as a
  // dead row. Suppressed while filtering: a search wants one flat result list.
  const recentRows = useMemo(() => {
    if (query.trim()) return []
    return recent.filter((r) => pinnedRepos.some(
      (p) => p.owner === r.owner && p.repo === r.repo))
  }, [recent, pinnedRepos, query])

  // Dropped from the main list only when the Recent section is actually shown,
  // so nothing is hidden when there is no second place to find it.
  const rest = useMemo(() => {
    if (recentRows.length < 2) return filtered
    return filtered.filter((r) => !recentRows.some(
      (x) => x.owner === r.owner && x.repo === r.repo))
  }, [filtered, recentRows])

  const pick = (r: ActiveRepo) => {
    setRecent(rememberRecentRepo(r))
    setActiveRepo(r)
    // The point of picking a repo is to see its PRs, so bring that list forward.
    setMainView('reviews')
    setListTab('pulls')
  }

  const label = activeRepo ? repoSlug(activeRepo) : i18nT('apps.codeReviewSage.components.repoSwitcher.pick_a_repo')

  return (
    <div className="px-2">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            aria-label={activeRepo
          ? i18nT('apps.codeReviewSage.components.repoSwitcher.repository_named', { name: label })
          : i18nT('apps.codeReviewSage.components.repoSwitcher.pick_a_repository')}
            className="w-full flex items-center gap-2.5 px-3 py-2.5 rounded-xl border border-border-strong bg-bg-elevated shadow-sm hover:border-accent hover:bg-bg-hover cursor-pointer outline-none transition-colors"
          >
            {activeRepo
              ? <GithubLogo size={18} className="flex-shrink-0" />
              : <FolderGit2 size={17} className="flex-shrink-0 text-muted" aria-hidden="true" />}
            <span
              className={
                'flex-1 min-w-0 truncate text-[14px] font-semibold text-left leading-5 '
                + (activeRepo ? 'text-text' : 'text-muted')
              }
              title={activeRepo ? label : undefined}
            >
              {label}
            </span>
            <ChevronDown size={15} className="text-muted flex-shrink-0" />
          </button>
        </DropdownMenuTrigger>

        <DropdownMenuContent align="start" side="bottom" sideOffset={6} className="w-[320px]">
          <DropdownMenuLabel className="text-[12px] uppercase tracking-[.04em]">
            {recentRows.length >= 2
              ? i18nT('apps.codeReviewSage.components.repoSwitcher.recent')
              : i18nT('apps.codeReviewSage.components.repoSwitcher.repositories')}
          </DropdownMenuLabel>

          {/* Only when it earns its place: a filter over three repos is clutter. */}
          {pinnedRepos.length > 6 && (
            <div className="px-2 pb-1.5">
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                // Radix moves focus to items on key events; typing must reach the box.
                onKeyDown={(e) => e.stopPropagation()}
                placeholder={i18nT('apps.codeReviewSage.components.repoSwitcher.filter_repos')}
                aria-label={i18nT('apps.codeReviewSage.components.repoSwitcher.filter_repositories')}
                className="w-full rounded-lg border border-border bg-card px-2.5 py-1.5 text-[13px] text-text placeholder:text-muted outline-none focus:border-accent"
              />
            </div>
          )}

          {pinnedLoading && (
            <div className="px-3 py-2 text-[12.5px] text-muted">{i18nT('apps.codeReviewSage.components.repoSwitcher.loading')}</div>
          )}
          {!pinnedLoading && pinnedRepos.length === 0 && (
            <div className="px-3 py-2 text-[12.5px] text-muted leading-[1.5]">
              {i18nT('apps.codeReviewSage.components.repoSwitcher.no_repos_yet_add_one_to_pick_from_the_repos_you')}
            </div>
          )}
          {!pinnedLoading && pinnedRepos.length > 0 && filtered.length === 0 && (
            <div className="px-3 py-2 text-[12.5px] text-muted">{i18nT('apps.codeReviewSage.components.repoSwitcher.no_repo_matches_that')}</div>
          )}

          {/* Two or more: with a single recent repo a "Recent" heading over one row
              that also appears below is noise. */}
          {recentRows.length >= 2 && (
            <>
              {recentRows.map((r) => {
                const isActive = !!activeRepo
                  && activeRepo.owner === r.owner && activeRepo.repo === r.repo
                return (
                  <DropdownMenuItem
                    key={`recent-${r.owner}/${r.repo}`}
                    onSelect={() => pick({ owner: r.owner, repo: r.repo })}
                  >
                    <Clock size={12} className="flex-shrink-0 text-muted" aria-hidden="true" />
                    <span className="flex-1 min-w-0 truncate" title={`${r.owner}/${r.repo}`}>
                      {r.owner}/<span className="font-medium">{r.repo}</span>
                    </span>
                    {isActive && <Check size={13} className="text-accent flex-shrink-0" />}
                  </DropdownMenuItem>
                )
              })}
              <DropdownMenuSeparator />
              <DropdownMenuLabel className="text-[12px] uppercase tracking-[.04em]">
                {i18nT('apps.codeReviewSage.components.repoSwitcher.all_repos')}
              </DropdownMenuLabel>
            </>
          )}

          {rest.map((r) => {
            const isActive = !!activeRepo
              && activeRepo.owner === r.owner && activeRepo.repo === r.repo
            return (
              <DropdownMenuItem
                key={`${r.owner}/${r.repo}`}
                onSelect={() => pick({ owner: r.owner, repo: r.repo })}
              >
                <GithubLogo size={13} className="flex-shrink-0" />
                <span className="flex-1 min-w-0 truncate" title={`${r.owner}/${r.repo}`}>
                  {r.owner}/<span className="font-medium">{r.repo}</span>
                </span>
                {isActive && <Check size={13} className="text-accent flex-shrink-0" />}
              </DropdownMenuItem>
            )
          })}

          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => openAddRepos()}>
            <Plus size={13} className="flex-shrink-0" />
            <span className="flex-1">{i18nT('apps.codeReviewSage.components.repoSwitcher.add_a_repo')}</span>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      {pinError && (
        <div className="px-1 pt-1 text-[11.5px] text-danger">{pinError.message}</div>
      )}
    </div>
  )
}
