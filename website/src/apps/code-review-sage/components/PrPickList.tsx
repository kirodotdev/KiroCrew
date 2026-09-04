// The active repo's open pull requests, in the MIDDLE column.
//
// The drill-down reads left to right: pick a repo in the rail, pick its PRs
// here, watch the review and read its report in the detail pane. The PR list
// used to live in the detail pane, which put the list and the report it produces
// in the same place and left this column showing nothing.
//
// Lifted out of the old NewReviewPanel, which is gone: the repo chooser it also
// carried is now the rail, so keeping it would have meant two ways to pick a
// repo and two PR lists.
import {
  ClipboardPaste, GitPullRequest, Link2, Loader2, RefreshCw, ScanSearch, Search,
} from 'lucide-react'
import { useMemo, useState } from 'react'

import { repoSlug, repoUrl, useSage, type ActiveRepo } from '../context'
import { changeKey, relativeAge } from '../lib/format'
import type { RepoPr } from '../lib/types'
import EmptyState from './EmptyState'
import ListSkeleton from './ListSkeleton'
import ReviewModelPicker from './ReviewModelPicker'

import { i18nT } from '../../../i18n/t'
/** Reviewing / reviewed / stale / new, as a single honest chip. `reviewing`
 *  outranks the rest: it is what is happening to this PR right now. */
function PrStateChip({ pr, reviewing }: { pr: RepoPr; reviewing?: boolean }) {
  if (reviewing) {
    return (
      <span
        title={i18nT('apps.codeReviewSage.components.prPickList.a_review_of_this_pull_request_is_running')}
        className="inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-accent-subtle text-accent whitespace-nowrap"
      >
        <Loader2
          size={9}
          className="animate-spin motion-reduce:animate-none"
          aria-hidden="true"
        />
        {i18nT('apps.codeReviewSage.components.prPickList.reviewing')}
      </span>
    )
  }
  if (pr.reviewed) {
    return (
      <span
        title={pr.reviewed_at
      ? i18nT('apps.codeReviewSage.components.prPickList.reviewed_age', { age: relativeAge(pr.reviewed_at) })
      : i18nT('apps.codeReviewSage.components.prPickList.already_reviewed')}
        className="inline-flex items-center text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-ok-subtle text-ok whitespace-nowrap"
      >
        {i18nT('apps.codeReviewSage.components.prPickList.reviewed')}
      </span>
    )
  }
  if (pr.reviewed_stale) {
    return (
      <span
        title={i18nT('apps.codeReviewSage.components.prPickList.reviewed_before_the_head_commit_has_moved_since')}
        className="inline-flex items-center text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-bg-elevated text-warn border border-border whitespace-nowrap"
      >
        {i18nT('apps.codeReviewSage.components.prPickList.updated')}
      </span>
    )
  }
  return (
    <span className="inline-flex items-center text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-accent-subtle text-accent whitespace-nowrap">
      {i18nT('apps.codeReviewSage.components.prPickList.new')}
    </span>
  )
}

/** The pasted-links escape hatch: a PR you were sent isn't necessarily in a repo
 *  you have added. Collapsed by default so it doesn't compete with the list. */
function PasteLinks() {
  const { startReviewLinks } = useSage()
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const busy = startReviewLinks.isPending

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1.5 text-[11.5px] text-muted bg-transparent hover:text-accent cursor-pointer self-start"
      >
        <ClipboardPaste size={12} /> {i18nT('apps.codeReviewSage.components.prPickList.paste_pr_links_instead')}
      </button>
    )
  }
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor="sage-paste-links" className="text-[11.5px] font-semibold text-muted">
        {i18nT('apps.codeReviewSage.components.prPickList.paste_pr_links')}
        <textarea
          id="sage-paste-links"
          rows={3}
          value={text}
          onChange={(e) => setText(e.target.value)}
          aria-label={i18nT('apps.codeReviewSage.components.prPickList.paste_pr_links')}
          placeholder={i18nT('apps.codeReviewSage.components.prPickList.one_or_more_github_pr_links')}
          className="mt-1 w-full rounded-lg border border-border bg-bg-elevated px-2 py-1.5 text-[12px] font-mono font-normal text-text outline-none focus-visible:border-accent resize-y block"
        />
      </label>
      <div className="flex items-center gap-2">
        <button
          onClick={() => startReviewLinks.mutate(text.trim())}
          disabled={busy || !text.trim()}
          className="inline-flex items-center gap-1.5 rounded-md bg-accent text-accent-fg px-2.5 py-1 text-[12px] font-medium border-none cursor-pointer hover:bg-accent-hover disabled:opacity-40 disabled:cursor-default transition-colors"
        >
          {busy
            ? <Loader2 size={12} className="animate-spin motion-reduce:animate-none" />
            : <Link2 size={12} />}
          {i18nT('apps.codeReviewSage.components.prPickList.review_these')}
        </button>
        <button
          onClick={() => { setOpen(false); setText('') }}
          className="text-[11.5px] text-muted bg-transparent hover:text-text cursor-pointer"
        >
          {i18nT('apps.codeReviewSage.components.prPickList.cancel')}
        </button>
      </div>
      {startReviewLinks.error && (
        <div className="text-[11.5px] text-danger">
          {(startReviewLinks.error as Error).message}
        </div>
      )}
    </div>
  )
}

export default function PrPickList() {
  const {
    activeRepo, prs, prsLoading, prsError, refreshPrs,
    startReview, startRepoReview, openAddRepos, selectedPr, selectPr,
    reviewingChangeUrls, reviewModel, setReviewModel,
  } = useSage()
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [query, setQuery] = useState('')

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return prs
    return prs.filter((p) => p.title.toLowerCase().includes(q)
      || String(p.number).includes(q)
      || (p.author ?? '').toLowerCase().includes(q))
  }, [prs, query])

  const unreviewed = prs.filter(
    (p) => !p.reviewed && !reviewingChangeUrls.has(changeKey(p.url)),
  ).length
  const busy = startReview.isPending || startRepoReview.isPending

  // Switching repo must not carry a selection over: those URLs belong to the
  // previous repo and would be silently reviewed alongside the new ones.
  const repoKey = activeRepo ? repoSlug(activeRepo) : ''
  const [lastRepoKey, setLastRepoKey] = useState(repoKey)
  const [confirmingAll, setConfirmingAll] = useState(false)
  if (repoKey !== lastRepoKey) {
    setLastRepoKey(repoKey)
    if (picked.size) setPicked(new Set())
    // The confirm names a count for the repo you were looking at, so it must not
    // survive a repo switch and authorize a different repo's reviews.
    if (confirmingAll) setConfirmingAll(false)
  }

  const toggle = (url: string) => setPicked((cur) => {
    const next = new Set(cur)
    if (next.has(url)) next.delete(url)
    else next.add(url)
    return next
  })

  if (!activeRepo) {
    return (
      <div className="flex flex-col min-h-0 h-full">
        <EmptyState
          icon={GitPullRequest}
          title={i18nT('apps.codeReviewSage.components.prPickList.pick_a_repo_to_see_its_pull_requests')}
          hint={i18nT('apps.codeReviewSage.components.prPickList.repos_live_in_the_sidebar')}
        >
          <button
            type="button"
            onClick={openAddRepos}
            className="inline-flex items-center gap-1.5 rounded-lg border border-accent bg-accent-subtle px-3 py-1.5 text-[12px] font-medium text-accent cursor-pointer transition-colors"
          >
            {i18nT('apps.codeReviewSage.components.prPickList.add_a_repo')}
          </button>
        </EmptyState>
      </div>
    )
  }

  return (
    <div className="flex flex-col min-h-0 h-full">
      <div className="px-2 pt-2 pb-1.5 flex-shrink-0 flex flex-col gap-2">
        <div className="flex items-center gap-1.5">
          <div className="flex items-center gap-1.5 rounded-lg border border-border bg-card px-2 flex-1 min-w-0 transition-colors focus-within:border-accent">
            <Search size={13} className="text-muted flex-shrink-0" aria-hidden="true" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              aria-label={i18nT('apps.codeReviewSage.components.prPickList.filter_pull_requests')}
              placeholder={i18nT('apps.codeReviewSage.components.prPickList.filter_pull_requests')}
              className="flex-1 min-w-0 bg-transparent border-0 py-1.5 text-[12.5px] text-text outline-none"
            />
          </div>
          <button
            onClick={refreshPrs}
            disabled={prsLoading}
            title={i18nT('apps.codeReviewSage.components.prPickList.re_read_open_prs_from_github')}
            aria-label={i18nT('apps.codeReviewSage.components.prPickList.refresh_pull_requests')}
            className="inline-flex items-center p-1.5 bg-transparent text-muted hover:text-text disabled:opacity-40 cursor-pointer flex-shrink-0"
          >
            <RefreshCw
              size={13}
              className={prsLoading ? 'animate-spin motion-reduce:animate-none' : ''}
            />
          </button>
        </div>

        <div className="flex items-center gap-1.5">
          <button
            onClick={() => startReview.mutate([...picked])}
            disabled={busy || picked.size === 0}
            className="flex-1 inline-flex items-center justify-center gap-1.5 rounded-md bg-accent text-accent-fg px-2 py-1.5 text-[12px] font-medium border-none cursor-pointer hover:bg-accent-hover disabled:opacity-40 disabled:cursor-default transition-colors"
          >
            {busy
              ? <Loader2 size={12} className="animate-spin motion-reduce:animate-none" />
              : <ScanSearch size={12} />}
            {/* One plural key, not three fragments: `picked.size || ''` renders
                "Review  selected" with no count and a double space when nothing is
                picked, and a translator cannot move the count within the sentence. */}
            {i18nT('apps.codeReviewSage.components.prPickList.review_selected_count',
              { count: picked.size })}
          </button>
          {confirmingAll ? (
            <>
              <span className="text-[12px] text-muted">
                {i18nT('apps.codeReviewSage.components.prPickList.confirm_review_all',
                  { count: unreviewed })}
              </span>
              <button
                type="button"
                onClick={() => {
                  setConfirmingAll(false)
                  startRepoReview.mutate({
                    repo: repoUrl(activeRepo as ActiveRepo), force: false,
                  })
                }}
                className="inline-flex items-center gap-1 rounded-md border border-accent bg-accent-subtle px-2 py-1.5 text-[12px] font-medium text-accent cursor-pointer hover:bg-accent/20 transition-colors flex-shrink-0"
              >
                {i18nT('apps.codeReviewSage.components.prPickList.review_these')}
              </button>
              <button
                type="button"
                onClick={() => setConfirmingAll(false)}
                className="rounded-md bg-transparent px-1.5 py-1.5 text-[12px] text-muted hover:text-text cursor-pointer border-none flex-shrink-0"
              >
                {i18nT('apps.codeReviewSage.components.prPickList.cancel')}
              </button>
            </>
          ) : (
          <button
            // Confirmed rather than fired on the first click: this starts one
            // long, paid review turn per unreviewed PR, and it sits beside the
            // "All" filter chip, so a misread click is both easy and expensive.
            // Cancelling afterwards is cooperative, so the guard goes in front.
            onClick={() => setConfirmingAll(true)}
            disabled={busy || unreviewed === 0}
            title={i18nT('apps.codeReviewSage.components.prPickList.review_every_open_pr_not_yet_reviewed_at_its_cur')}
            className="inline-flex items-center gap-1 rounded-md border border-border bg-transparent px-2 py-1.5 text-[12px] text-text cursor-pointer hover:bg-bg-hover disabled:opacity-40 disabled:cursor-default transition-colors flex-shrink-0"
          >
            {i18nT('apps.codeReviewSage.components.prPickList.review_all_count',
              { count: unreviewed })}
          </button>
          )}
        </div>

        <ReviewModelPicker
          value={reviewModel}
          onChange={setReviewModel}
          disabled={busy}
        />

        <PasteLinks />

        {startRepoReview.data?.status === 'noop' && (
          <div className="text-[11.5px] text-muted">{startRepoReview.data?.message}</div>
        )}
        {startReview.error && (
          <div className="text-[11.5px] text-danger">
            {(startReview.error as Error).message}
          </div>
        )}
        {startRepoReview.error && (
          <div className="text-[11.5px] text-danger">
            {(startRepoReview.error as Error).message}
          </div>
        )}
        {prsError && <div className="text-[11.5px] text-danger">{prsError.message}</div>}
      </div>

      <div className="relative flex-1 min-h-0">
        <div
          className="absolute inset-0 overflow-y-auto scrollbar-none px-2 pb-2 flex flex-col gap-1.5"
          style={{ scrollbarWidth: 'none' }}
        >
          {prsLoading && <ListSkeleton count={5} />}
          {!prsLoading && filtered.length === 0 && (
            <EmptyState
              icon={GitPullRequest}
              title={query.trim()
                ? i18nT('apps.codeReviewSage.components.prPickList.no_prs_match_filter')
                : i18nT('apps.codeReviewSage.components.prPickList.no_open_prs_here')}
            />
          )}
          {!prsLoading && filtered.map((pr) => {
            const reviewing = reviewingChangeUrls.has(changeKey(pr.url))
            // A PR under review cannot be queued again: the backend's in-flight
            // claim would refuse the duplicate, so a tickable box would lie.
            const on = picked.has(pr.url) && !reviewing
            const isOpen = selectedPr?.url === pr.url
            const inputId = `sage-pr-${pr.number}`
            return (
              // The checkbox BATCHES; clicking the row OPENS the PR and its
              // review. These were one <label> before, which meant there was no
              // way to look at a PR without also queueing it.
              <div
                key={pr.url}
                className={`flex items-start gap-2 rounded-lg border px-2.5 py-2 transition-colors ${
                  isOpen
                    ? 'border-accent bg-accent-subtle'
                    : on
                      ? 'border-accent/50 bg-card'
                      : 'border-border bg-card hover:bg-bg-hover'
                }`}
              >
                <input
                  id={inputId}
                  type="checkbox"
                  checked={on}
                  disabled={reviewing}
                  onChange={() => toggle(pr.url)}
                  aria-label={reviewing
                    ? i18nT('apps.codeReviewSage.components.prPickList.pr_already_being_reviewed', { number: pr.number })
                    : i18nT('apps.codeReviewSage.components.prPickList.review_pr', { number: pr.number, title: pr.title })}
                  className={`mt-1 flex-shrink-0 ${
                    reviewing ? 'cursor-not-allowed opacity-40' : 'cursor-pointer'
                  }`}
                  style={{ accentColor: 'var(--accent)' }}
                />
                <button
                  type="button"
                  onClick={() => selectPr(pr)}
                  aria-current={isOpen ? 'true' : undefined}
                  aria-label={i18nT('apps.codeReviewSage.components.prPickList.open_pr', { number: pr.number, title: pr.title })}
                  className="flex-1 min-w-0 text-left bg-transparent cursor-pointer"
                >
                  <span className="flex items-center gap-1.5 text-[11.5px] text-muted">
                    <span className="font-bold text-accent">#{pr.number}</span>
                    {pr.author && <span className="truncate">· {pr.author}</span>}
                    {pr.draft && (
                      <span className="px-1 py-0.5 rounded-full bg-bg-elevated border border-border text-[10px]">
                        {i18nT('apps.codeReviewSage.components.prPickList.draft')}
                      </span>
                    )}
                    <span className="ml-auto flex-shrink-0">{relativeAge(pr.updated_at)}</span>
                  </span>
                  <span className="block text-[13px] leading-snug text-text mt-0.5 line-clamp-2">
                    {pr.title}
                  </span>
                  <span className="block mt-1">
                    <PrStateChip pr={pr} reviewing={reviewing} />
                  </span>
                </button>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
