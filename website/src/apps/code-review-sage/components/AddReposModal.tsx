// "Add repos" modal — pick from the repos you can actually reach, or type one in.
//
// The rail used to offer only a URL field while its own copy promised you could
// "pick from the repos you have worked on recently", so the discovery that
// already existed was unreachable outside the New-review composer.
//
// Two lists, because they answer different questions and neither subsumes the
// other: RECENT comes from the GitHub event feed (what you have touched lately,
// ranked by contribution), ALL comes from the repos API (what you can reach at
// all — including a repo you own but have not pushed to inside the activity
// window). Manual entry always stays available: both lists are capped and can be
// truncated, and neither covers a repo someone just sent you a link to.
//
// Shell mechanics (app-scoped `absolute inset-0` backdrop, Framer exit
// animation) mirror Issue Radar's ConnectRepoModal so the two builtins behave
// identically. The keyboard behaviour — capture-phase Escape, the Tab trap and
// focus restore — comes from the shared `useDialogFocusTrap` hook.
import { AnimatePresence, motion } from 'framer-motion'
import {
  AlertCircle, Check, FolderGit2, GitBranch, Loader2, Lock, Plus, RefreshCw, Search, Star, Trash2, X,
} from 'lucide-react'
import { useCallback, useMemo, useRef, useState, type ReactNode } from 'react'

import Clickable from '../../../components/Clickable'

import EmptyState from '../components/EmptyState'
import ListSkeleton from '../components/ListSkeleton'
import { useSage } from '../context'
import { relativeAge } from '../lib/format'

import { useDialogFocusTrap } from '../../../hooks/useDialogFocusTrap'
import { i18nT } from '../../../i18n/t'

/** One selectable repo row, shared by both lists. */
function RepoRow({
  fullName, meta, pinned, onPin, badges,
}: {
  fullName: string
  meta?: string
  pinned: boolean
  onPin: () => void
  badges?: ReactNode
}) {
  return (
    <li>
      <button
        type="button"
        onClick={onPin}
        disabled={pinned}
        aria-label={pinned
      ? i18nT('apps.codeReviewSage.components.addReposModal.already_added', { repo: fullName })
      : i18nT('apps.codeReviewSage.components.addReposModal.add_repo', { repo: fullName })}
        className={`w-full flex items-center gap-2 rounded-lg border px-2.5 py-2 text-left transition-colors ${
          pinned
            ? 'border-border bg-bg-elevated cursor-default'
            : 'border-border bg-card hover:bg-bg-hover hover:border-accent cursor-pointer'
        }`}
      >
        <FolderGit2
          size={14}
          className={`flex-shrink-0 ${pinned ? 'text-muted' : 'text-accent'}`}
          aria-hidden="true"
        />
        <span className="flex-1 min-w-0">
          <span className="block text-[13.5px] text-text truncate">{fullName}</span>
          {meta && <span className="block text-[11.5px] text-muted mt-0.5">{meta}</span>}
        </span>
        {badges}
        {pinned ? (
          <span className="flex-shrink-0 inline-flex items-center gap-1 text-[11px] text-ok">
            <Check size={12} aria-hidden="true" /> {i18nT('apps.codeReviewSage.components.addReposModal.added')}
          </span>
        ) : (
          <Plus size={13} className="flex-shrink-0 text-muted" aria-hidden="true" />
        )}
      </button>
    </li>
  )
}

function SectionHead({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex items-baseline gap-2 mt-1">
      <h2 className="text-[11px] uppercase tracking-wider text-muted font-medium">{title}</h2>
      {hint && <span className="text-[11.5px] text-muted opacity-70">{hint}</span>}
    </div>
  )
}

/** `gh` isn't installed or signed in — a normal first-run state, not an error. */
function GhNotice({ message }: { message?: string }) {
  return (
    <div className="rounded-lg border border-border bg-bg-elevated px-3 py-2.5 text-[12.5px] leading-[1.6]">
      <span className="inline-flex items-center gap-1.5 text-warn font-medium">
        <AlertCircle size={13} aria-hidden="true" /> {i18nT('apps.codeReviewSage.components.addReposModal.github_cli_not_ready')}
      </span>
      <div className="text-muted mt-1">
        {/* One sentence in one key, with the command on its own line: the copy
            used to be four sibling keys wrapped around two <code> elements,
            which no translator could reorder. */}
        {i18nT('apps.codeReviewSage.components.addReposModal.setup_hint')}
        <div className="mt-1">
          <code className="font-mono">
            {i18nT('apps.codeReviewSage.components.addReposModal.gh_auth_login')}
          </code>
        </div>
      </div>
      {message && <div className="text-muted opacity-70 mt-1 break-words">{message}</div>}
    </div>
  )
}

export default function AddReposModal({ onClose }: { onClose: () => void }) {
  const {
    pinnedRepos, pinRepo, pinRepoUrl, pinError, unpinRepo,
    recent, recentLoading, recentError,
    mine, mineLoading, mineError, refreshMine,
  } = useSage()

  const [query, setQuery] = useState('')
  const [manual, setManual] = useState('')

  const dialogRef = useRef<HTMLDivElement>(null)

  const requestClose = useCallback(() => onClose(), [onClose])

  // Focus-in, focus-restore, Escape and the Tab trap all come from the shared
  // hook. This file used to hand-roll them; main extracted the same three pieces
  // (plus `preventScroll`, which stops the page behind the overlay scrolling
  // during the entrance animation) so every dialog behaves identically.
  useDialogFocusTrap(dialogRef, requestClose)

  const pinnedKeys = useMemo(
    () => new Set(pinnedRepos.map((r) => `${r.owner}/${r.repo}`.toLowerCase())),
    [pinnedRepos],
  )
  const isPinned = (fullName: string) => pinnedKeys.has(fullName.toLowerCase())

  const q = query.trim().toLowerCase()
  const match = (fullName: string) => !q || fullName.toLowerCase().includes(q)

  const recentRows = (recent?.repos ?? []).filter((r) => match(r.full_name))
  // Anything already in the recent list is omitted from the full list so the two
  // sections don't show the same repo twice.
  const recentKeys = new Set((recent?.repos ?? []).map((r) => r.full_name.toLowerCase()))
  const mineRows = (mine?.repos ?? [])
    .filter((r) => match(r.full_name) && !recentKeys.has(r.full_name.toLowerCase()))

  const setupRequired = recent?.setup_required || mine?.setup_required
  const loading = recentLoading || mineLoading
  const err = recentError ?? mineError ?? pinError

  const submitManual = () => {
    const v = manual.trim()
    if (!v) return
    // Accept a full URL, a bare owner/repo, or a PULL REQUEST url — the backend
    // validates all three and, for a PR link, pins its repo and opens the PR.
    pinRepoUrl(v.includes('://') ? v : `https://github.com/${v.replace(/^\/+|\/+$/g, '')}`)
    setManual('')
  }

  return (
    <AnimatePresence>
      {/* Backdrop and dialog are SIBLINGS: nesting the dialog inside the
        * clickable backdrop would give every control a `button` ancestor, which
        * assistive tech can flatten into one widget and suppress the descendant
        * semantics. `absolute` (not `fixed`) scopes the blur to the app area. */}
      <div className="absolute inset-0 z-50 flex items-center justify-center p-4">
        <Clickable
          className="absolute inset-0 bg-bg/50 backdrop-blur-sm"
          onClick={requestClose}
          aria-label={i18nT('apps.codeReviewSage.components.addReposModal.close_add_repos_dialog')}
        />
        <motion.div
          ref={dialogRef}
          role="dialog"
          aria-modal="true"
          aria-label={i18nT('apps.codeReviewSage.components.addReposModal.add_repos')}
          tabIndex={-1}
          initial={{ opacity: 0, y: 8, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 8, scale: 0.98 }}
          transition={{ duration: 0.18, ease: 'easeOut' }}
          className="relative w-[680px] max-w-full h-[620px] max-h-full border border-border rounded-[14px] bg-card flex flex-col shadow-2xl outline-none overflow-hidden"
          onKeyDown={(e) => e.stopPropagation()}
        >
          <header className="px-6 pt-5 pb-4 border-b border-border flex-shrink-0">
            <h1 className="text-[19px] font-bold leading-tight text-text-strong pr-8">
              {i18nT('apps.codeReviewSage.components.addReposModal.add_repos')}
            </h1>
            <p className="text-[12.5px] text-muted mt-1.5 leading-[1.5]">
              {i18nT('apps.codeReviewSage.components.addReposModal.pick_from_the_repos_you_can_reach_or_type_one_in')}
            </p>
            <button
              onClick={requestClose}
              aria-label={i18nT('apps.codeReviewSage.components.addReposModal.close')}
              className="absolute top-3 right-3 p-1.5 rounded-md text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-0"
            >
              <X size={16} />
            </button>
          </header>

          {/* Inputs stay pinned; only the lists scroll, so the manual field and
              the filter are always reachable however long the lists get. */}
          <div className="px-6 pt-4 pb-3 flex flex-col gap-2.5 flex-shrink-0">
            {/* Manual entry FIRST: it always works, even with no gh, and it is
                the answer when a repo is missing from both capped lists. */}
            <div className="flex items-center gap-2">
              <div className="flex items-center gap-1.5 rounded-lg border border-border bg-bg-elevated px-2.5 flex-1 min-w-0 transition-colors focus-within:border-accent">
                <GitBranch size={13} className="text-muted flex-shrink-0" aria-hidden="true" />
                <input
                  value={manual}
                  onChange={(e) => setManual(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') submitManual() }}
                  aria-label={i18nT('apps.codeReviewSage.components.addReposModal.repository_or_pull_request_url_or_owner_repo')}
                  placeholder={i18nT('apps.codeReviewSage.components.addReposModal.owner_repo_a_repo_url_or_paste_a_pull_request_li')}
                  className="flex-1 min-w-0 bg-transparent border-0 py-2 text-[13px] font-mono text-text outline-none"
                />
              </div>
              <button
                type="button"
                onClick={submitManual}
                disabled={!manual.trim()}
                className="inline-flex items-center gap-1.5 rounded-md bg-accent text-accent-fg px-3 py-2 text-[12.5px] font-medium border-none cursor-pointer hover:bg-accent-hover disabled:opacity-40 disabled:cursor-default transition-colors"
              >
                <Plus size={13} aria-hidden="true" /> {i18nT('apps.codeReviewSage.components.addReposModal.add')}
              </button>
            </div>

            <div className="flex items-center gap-2">
              <div className="flex items-center gap-1.5 rounded-lg border border-border bg-bg-elevated px-2.5 flex-1 min-w-0 transition-colors focus-within:border-accent">
                <Search size={13} className="text-muted flex-shrink-0" aria-hidden="true" />
                <input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  aria-label={i18nT('apps.codeReviewSage.components.addReposModal.filter_your_repos')}
                  placeholder={i18nT('apps.codeReviewSage.components.addReposModal.filter_your_repos')}
                  className="flex-1 min-w-0 bg-transparent border-0 py-1.5 text-[13px] text-text outline-none"
                />
              </div>
              <button
                type="button"
                onClick={refreshMine}
                disabled={loading}
                title={i18nT('apps.codeReviewSage.components.addReposModal.re_read_your_repos_from_github')}
                aria-label={i18nT('apps.codeReviewSage.components.addReposModal.refresh_your_repos')}
                className="inline-flex items-center p-1.5 bg-transparent text-muted hover:text-text disabled:opacity-40 cursor-pointer"
              >
                <RefreshCw
                  size={14}
                  className={loading ? 'animate-spin motion-reduce:animate-none' : ''}
                />
              </button>
            </div>
          </div>

          <div className="flex-1 min-h-0 overflow-y-auto scrollbar-none px-6 pb-5 flex flex-col gap-2.5">
            {setupRequired && <GhNotice message={recent?.error ?? mine?.error} />}
            {err && !setupRequired && (
              <div className="text-[12.5px] text-danger">{err.message}</div>
            )}

            {loading && !setupRequired && (
              <>
                <div className="inline-flex items-center gap-1.5 text-[12.5px] text-muted">
                  <Loader2 size={13} className="animate-spin motion-reduce:animate-none" />
                  {i18nT('apps.codeReviewSage.components.addReposModal.reading_your_repos_from_github')}
                </div>
                <ListSkeleton count={5} />
              </>
            )}

            {/* Your current repos, with removal. This is the repo-MANAGEMENT
                surface; the rail's dropdown only selects, because an interactive
                control nested in a menu item is invalid a11y (and Radix closes
                the menu out from under it). */}
            {pinnedRepos.length > 0 && (
              <>
                <SectionHead title={i18nT('apps.codeReviewSage.components.addReposModal.your_repos')} hint={i18nT('apps.codeReviewSage.components.addReposModal.pinned_to_the_sidebar')} />
                <ul className="flex flex-col gap-1.5 list-none p-0 m-0">
                  {pinnedRepos.map((r) => (
                    <li
                      key={`${r.owner}/${r.repo}`}
                      className="flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-2"
                    >
                      <span className="flex-1 min-w-0 truncate text-[13px] text-text">
                        {r.owner}/<span className="font-medium">{r.repo}</span>
                      </span>
                      <button
                        type="button"
                        onClick={() => unpinRepo(r.owner, r.repo)}
                        aria-label={i18nT('apps.codeReviewSage.components.addReposModal.remove_repo', { repo: `${r.owner}/${r.repo}` })}
                        className="flex-shrink-0 inline-flex items-center gap-1 rounded-md px-1.5 py-1 bg-transparent text-[12px] text-muted hover:text-danger cursor-pointer"
                      >
                        <Trash2 size={12} aria-hidden="true" />
                        {i18nT('apps.codeReviewSage.components.addReposModal.remove')}
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            )}

            {!loading && !setupRequired && (
              <>
                {recentRows.length > 0 && (
                  <>
                    <SectionHead title={i18nT('apps.codeReviewSage.components.addReposModal.recently_worked_on')} hint={i18nT('apps.codeReviewSage.components.addReposModal.based_on_recent_github_activity')} />
                    <ul className="flex flex-col gap-1.5 list-none p-0 m-0">
                      {recentRows.map((r) => (
                        <RepoRow
                          key={r.full_name}
                          fullName={r.full_name}
                          meta={i18nT('apps.codeReviewSage.components.addReposModal.contributed_events',
                            { age: relativeAge(r.last_contributed_at),
                              count: r.contribution_count })}
                          pinned={isPinned(r.full_name)}
                          onPin={() => pinRepo(r.owner, r.repo)}
                        />
                      ))}
                    </ul>
                  </>
                )}

                {mineRows.length > 0 && (
                  <>
                    <SectionHead
                      title={i18nT('apps.codeReviewSage.components.addReposModal.all_your_repos')}
                      hint={mine?.truncated
                        ? i18nT('apps.codeReviewSage.components.addReposModal.newest_100_by_push_date')
                        : undefined}
                    />
                    <ul className="flex flex-col gap-1.5 list-none p-0 m-0">
                      {mineRows.map((r) => (
                        <RepoRow
                          key={r.full_name}
                          fullName={r.full_name}
                          meta={r.pushed_at
                            ? i18nT('apps.codeReviewSage.components.addReposModal.pushed_age',
                                    { age: relativeAge(r.pushed_at) })
                            : undefined}
                          pinned={isPinned(r.full_name)}
                          onPin={() => pinRepo(r.owner, r.repo)}
                          badges={(
                            <span className="flex-shrink-0 inline-flex items-center gap-1.5">
                              {r.private && (
                                <span title={i18nT('apps.codeReviewSage.components.addReposModal.private')} className="inline-flex items-center text-muted">
                                  <Lock size={11} aria-hidden="true" />
                                  <span className="sr-only">{i18nT('apps.codeReviewSage.components.addReposModal.private')}</span>
                                </span>
                              )}
                              {r.archived && (
                                <span className="text-[10.5px] px-1.5 py-0.5 rounded-full bg-bg-elevated border border-border text-muted">
                                  {i18nT('apps.codeReviewSage.components.addReposModal.archived')}
                                </span>
                              )}
                              {!r.can_push && (
                                <span
                                  title={i18nT('apps.codeReviewSage.components.addReposModal.read_only_access')}
                                  className="text-[10.5px] px-1.5 py-0.5 rounded-full bg-bg-elevated border border-border text-muted"
                                >
                                  {i18nT('apps.codeReviewSage.components.addReposModal.read_only')}
                                </span>
                              )}
                            </span>
                          )}
                        />
                      ))}
                    </ul>
                  </>
                )}

                {recentRows.length === 0 && mineRows.length === 0 && (
                  <EmptyState
                    icon={Star}
                    title={q
                          ? i18nT('apps.codeReviewSage.components.addReposModal.no_repos_match_filter')
                          : i18nT('apps.codeReviewSage.components.addReposModal.no_repos_for_account')}
                    hint={i18nT('apps.codeReviewSage.components.addReposModal.add_one_by_url_above')}
                  />
                )}

                {(mine?.truncated || recent?.truncated) && (
                  <div className="text-[11.5px] text-muted opacity-80 leading-[1.5] mt-1">
                    {i18nT('apps.codeReviewSage.components.addReposModal.these_lists_are_capped_so_a_repo_you_have_not_to')}
                  </div>
                )}
              </>
            )}
          </div>
        </motion.div>
      </div>
    </AnimatePresence>
  )
}
