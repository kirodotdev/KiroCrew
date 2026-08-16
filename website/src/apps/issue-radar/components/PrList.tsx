import { useEffect, useState } from 'react'
import { motion, AnimatePresence, useReducedMotion } from 'framer-motion'
import { Virtuoso } from 'react-virtuoso'
import {
  RefreshCw, Search, X, GitPullRequest, GitMerge, GitPullRequestClosed, GitPullRequestDraft,
  CheckCircle2, XCircle, CircleSlash, Loader2, FileDiff,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { useIssueRadar } from '../context'
import { relativeTimeOrDate, relativeTime } from '../lib/format'
import type { PullRequest } from '../api'
import LabelChip from './LabelChip'
import ListSkeleton from './ListSkeleton'
import ListEmptyState from './ListEmptyState'
import PrBulkBar from './PrBulkBar'
import { providerTerms } from '../lib/links'

import { i18nT } from '../../../i18n/t'
/** Above this many rendered rows we skip the per-card enter/layout animation
 * (same rationale as IssueList — Framer's layout pass janks on large lists). */
const ANIM_CAP = 200

/** Icon + colour for a PR row/detail by its lifecycle: merged (purple) →
 * closed-unmerged (red) → draft (muted) → open (green). Derived purely from the
 * list fields (state + draft + merged_at), so no detail fetch is needed. */
function prStateVisual(pr: { state: string; draft?: boolean; merged_at?: string | null }):
  { Icon: LucideIcon; color: string; label: string } {
  if (pr.merged_at) return { Icon: GitMerge, color: 'text-aim', label: 'Merged' }
  if (pr.state === 'closed') return { Icon: GitPullRequestClosed, color: 'text-danger', label: 'Closed' }
  if (pr.draft) return { Icon: GitPullRequestDraft, color: 'text-muted', label: 'Draft' }
  return { Icon: GitPullRequest, color: 'text-ok', label: 'Open' }
}

/** The four check buckets in fixed render order, so a card's badges never
 * reshuffle between renders and the actionable states read first. */
const CHECK_BADGES = [
  { key: 'failure', Icon: XCircle, color: 'text-danger', label: 'failing' },
  { key: 'running', Icon: Loader2, color: 'text-accent', label: 'running' },
  { key: 'success', Icon: CheckCircle2, color: 'text-ok', label: 'passing' },
  { key: 'other', Icon: CircleSlash, color: 'text-muted', label: 'skipped / neutral' },
] as const

/** Per-bucket check counts for a card ("2 ✗  1 ⟳  34 ✓"). Buckets with a zero
 * count are omitted so a green PR stays quiet. Falls back to the aggregate
 * rollup dot when the counts are unavailable (a cached row written before the
 * counts shipped, or an enrichment call that failed) — and also when the tally
 * is TRUNCATED: a PR with more checks than one API page has an incomplete count,
 * and the omitted page could hold the only failure, so showing "34 passing"
 * there would be a confident lie. The aggregate rollup covers every check. */
function ChecksTally({ pr }: { pr: PullRequest }) {
  const counts = pr.checks_counts
  const total = counts ? CHECK_BADGES.reduce((n, b) => n + (counts[b.key] ?? 0), 0) : 0
  if (!counts || total === 0 || pr.checks_truncated) return <ChecksDot state={pr.checks_state} />
  return (
    <span className="inline-flex items-center gap-2">
      {CHECK_BADGES.map(({ key, Icon, color, label }) => {
        const n = counts[key] ?? 0
        if (n === 0) return null
        return (
          <span
            key={key}
            title={`${n} ${label}`}
            className={`inline-flex items-center gap-1 ${color}`}
          >
            <span className="tabular-nums font-medium">{n}</span>
            <Icon size={12} className={key === 'running' ? 'animate-spin' : ''} />
          </span>
        )
      })}
    </span>
  )
}

/** GitHub-style five-block diff bar: how much of the change is additions vs
 * removals, at a glance. Filled blocks are proportional; a non-zero side always
 * claims at least one block so a tiny removal is never rounded away. */
function DiffBar({ add, del }: { add: number; del: number }) {
  const total = add + del
  if (total === 0) return null
  let green = Math.round((add / total) * DIFF_BLOCKS)
  if (add > 0 && green === 0) green = 1
  if (del > 0 && green === DIFF_BLOCKS) green = DIFF_BLOCKS - 1
  return (
    <span className="inline-flex items-center gap-[2px]" aria-hidden="true">
      {Array.from({ length: DIFF_BLOCKS }, (_, i) => (
        <span
          key={i}
          className={`w-[6px] h-[6px] rounded-[1px] ${i < green ? 'bg-ok' : 'bg-danger'}`}
        />
      ))}
    </span>
  )
}

const DIFF_BLOCKS = 5

/** Aggregate check-rollup dot for a card: only the state that matters, no text.
 * Mirrors the detail pane's bucket colours so red means the same thing in both
 * places. Renders nothing when the PR has no checks. */
function ChecksDot({ state }: { state: PullRequest['checks_state'] }) {
  if (!state) return null
  const visual = {
    failure: { Icon: XCircle, color: 'text-danger', label: 'checks failing' },
    running: { Icon: Loader2, color: 'text-accent', label: 'checks running' },
    success: { Icon: CheckCircle2, color: 'text-ok', label: 'checks passing' },
    other: { Icon: CircleSlash, color: 'text-muted', label: 'checks skipped / neutral' },
  }[state]
  const { Icon, color, label } = visual
  return (
    <span title={label} aria-label={label} className={`inline-flex items-center ${color}`}>
      <Icon size={12} className={state === 'running' ? 'animate-spin' : ''} />
    </span>
  )
}

/** Middle column for the pull-request view: a search box, the filtered + sorted
 * PR list (cards animate as the search narrows them), and a footer carrying the
 * count, the time since the last refresh, and the refresh button. The PR
 * analogue of IssueList. `resizing` (true while the width handle is dragged)
 * switches off the card layout animation, which would otherwise scale-distort
 * the card text on every pointer move — see IssueList. */
export default function PrList({ resizing = false }: { resizing?: boolean }) {
  const {
    filteredPulls, sortedPulls, pullsLoading, pullsError, pullsPartial,
    prStateFilter, colorByName,
    selectedPull, setSelectedPull, refreshPulls, pullsRefreshing,
    prQuery, setPrQuery, pullsUpdatedAt, prPersonFilterActive, prSearchTruncatedAt,
    active, canWrite, checkedPulls, togglePullChecked, clearCheckedPulls,
  } = useIssueRadar()
  // Provider vocabulary: GitLab calls these merge requests, and calling them
  // pull requests in a GitLab workspace is simply wrong copy.
  const terms = providerTerms(active)

  const reduce = useReducedMotion()
  const animate = !reduce && sortedPulls.length <= ANIM_CAP

  const [, tick] = useState(0)
  useEffect(() => {
    const id = setInterval(() => tick((t) => t + 1), 30_000)
    return () => clearInterval(id)
  }, [])

  // Escape clears a bulk selection — the standard way out of a multi-select, and
  // the fastest way to disarm an accidental "select all" before touching anything.
  useEffect(() => {
    if (checkedPulls.size === 0) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') clearCheckedPulls()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [checkedPulls.size, clearCheckedPulls])

  const cardClass = (isSel: boolean) =>
    `w-full text-left rounded-lg border p-2.5 cursor-pointer bg-card hover:bg-bg-hover transition-colors ${
      isSel ? 'border-accent' : 'border-border'
    }`

  /** One row: the select checkbox beside the card, not inside it.
   *
   * A checkbox nested in the card's `<button>` would be invalid HTML (interactive
   * content inside a button) and unreachable by keyboard, so the row is a flex
   * container holding two siblings. The checkbox only renders on a writable repo —
   * on a read-only one every bulk action would 403, so offering the selection at
   * all would be a dead end.
   */
  const row = (pr: PullRequest, children: React.ReactNode) => {
    if (!canWrite) return children
    return (
      <div className="flex items-start gap-1.5">
        <input
          type="checkbox"
          checked={checkedPulls.has(pr.number)}
          onChange={() => togglePullChecked(pr.number)}
          aria-label={i18nT('apps.issueRadar.components.prList.select_for_bulk', { subject: terms.changeRequestShort, number: pr.number })}
          className="mt-3 flex-shrink-0 cursor-pointer accent-[var(--accent)]"
        />
        <div className="min-w-0 flex-1">{children}</div>
      </div>
    )
  }

  const cardInner = (pr: PullRequest) => {
    const { Icon, color } = prStateVisual(pr)
    const add = pr.additions ?? 0
    const del = pr.deletions ?? 0
    const files = pr.changed_files ?? 0
    const hasDiff = add > 0 || del > 0 || files > 0
    const hasChecks = Boolean(pr.checks_state) || Boolean(pr.checks_counts)
    return (
      <>
        <div className="flex items-center justify-between gap-2 text-[12px] text-muted mb-1">
          <span className="inline-flex items-center gap-1.5 truncate">
            <Icon size={13} className={`flex-shrink-0 ${color}`} />
            <span className="font-bold text-accent">#{pr.number}</span>
            {pr.author ? <span className="truncate">· {pr.author}</span> : null}
          </span>
          <span className="flex-shrink-0">{relativeTimeOrDate(pr.updated_at)}</span>
        </div>
        <div className="text-[14px] leading-snug text-text line-clamp-2">{pr.title}</div>
        {pr.labels.length > 0 && (
          <div className="flex items-center gap-1.5 mt-1.5 flex-wrap">
            {pr.labels.map((name) => (
              <LabelChip key={name} name={name} color={colorByName.get(name) ?? '888888'} small />
            ))}
          </div>
        )}
        {/* Bottom row: diff shape on the left (files touched, a proportional
            add/remove bar, then the raw counts), per-bucket check tally on the
            right. Both come from the list enrichment, so the row is omitted
            entirely when neither is available. */}
        {(hasDiff || hasChecks) && (
          <div className="flex items-center gap-2 mt-1.5 text-[11px] tabular-nums">
            {hasDiff && (
              <span className="inline-flex items-center gap-2 text-muted">
                {files > 0 && (
                  <span
                    className="inline-flex items-center gap-1"
                    title={`${files} file${files === 1 ? '' : 's'} changed`}
                  >
                    <FileDiff size={11} />
                    {files}
                  </span>
                )}
                <DiffBar add={add} del={del} />
                <span className="inline-flex items-center gap-1.5">
                  {add > 0 && <span className="text-ok">+{add}</span>}
                  {del > 0 && <span className="text-danger">−{del}</span>}
                </span>
              </span>
            )}
            <span className="ml-auto flex-shrink-0"><ChecksTally pr={pr} /></span>
          </div>
        )}
      </>
    )
  }

  const lastUpdated = relativeTime(pullsUpdatedAt)

  return (
    <section className="flex flex-col min-h-0 h-full">
      <div className="px-2 pt-2 pb-1.5 flex-shrink-0">
        <div className="flex items-center gap-1.5 rounded-lg border border-border bg-card px-2.5 transition-colors focus-within:border-accent">
          <Search size={14} className="flex-shrink-0 text-muted opacity-60" />
          <input
            value={prQuery}
            onChange={(e) => setPrQuery(e.target.value)}
            placeholder={i18nT('apps.issueRadar.components.prList.search', { label: terms.changeRequestPluralTitle })}
            aria-label={i18nT('apps.issueRadar.components.prList.search_2', { label: terms.changeRequestPluralTitle })}
            className="flex-1 min-w-0 bg-transparent py-2.5 text-[13px] text-text placeholder:text-muted outline-none"
          />
          {prQuery && (
            <button
              onClick={() => setPrQuery('')}
              title={i18nT('apps.issueRadar.components.prList.clear_search')}
              aria-label={i18nT('apps.issueRadar.components.prList.clear_search')}
              className="flex-shrink-0 cursor-pointer bg-transparent leading-none text-muted hover:text-text"
            >
              <X size={13} />
            </button>
          )}
        </div>
      </div>

      <PrBulkBar />

      <div className="relative flex-1 min-h-0">
        {pullsLoading && (
          <div className="absolute inset-0 overflow-y-auto scrollbar-none px-2 pb-2 flex flex-col gap-2" style={{ scrollbarWidth: 'none' }}>
            <ListSkeleton />
          </div>
        )}
        {pullsError && <div className="px-3 py-2 text-[14px] text-danger">{pullsError.message}</div>}
        {!pullsLoading && filteredPulls.length === 0 && (
          <div className="px-2 pb-2">
            <ListEmptyState searching={Boolean(prQuery.trim())} label={terms.changeRequestPluralTitle} />
          </div>
        )}
        {!pullsLoading && sortedPulls.length > 0 && (
          animate ? (
            // Small list: plain animated flow (see IssueList — deliberately not
            // virtualized, AnimatePresence needs all siblings mounted; ANIM_CAP-bounded).
            <div className="absolute inset-0 overflow-y-auto scrollbar-none px-2 pb-2 flex flex-col gap-2" style={{ scrollbarWidth: 'none' }}>
              <AnimatePresence initial={false} mode="popLayout">
                {sortedPulls.map((pr) => (
                  <motion.div
                    key={pr.number}
                    // 'position' (not the default size+position): a size-animating
                    // layout pass distorts the card's text with a scale transform
                    // whenever the column rewraps. Off entirely mid-resize.
                    layout={resizing ? false : 'position'}
                    initial={{ opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, scale: 0.97 }}
                    transition={{
                      layout: { type: 'spring', stiffness: 550, damping: 40 },
                      duration: 0.18,
                      ease: [0.16, 1, 0.3, 1],
                    }}
                  >
                    {row(pr, (
                      <button
                        onClick={() => setSelectedPull(pr.number)}
                        className={cardClass(selectedPull === pr.number)}
                      >
                        {cardInner(pr)}
                      </button>
                    ))}
                  </motion.div>
                ))}
              </AnimatePresence>
            </div>
          ) : (
            // Large list: virtualize so only visible rows are DOM nodes. Row gap is
            // a per-row bottom padding (Virtuoso positions rows absolutely, so a
            // container flex `gap` does not apply).
            <Virtuoso
              className="absolute inset-0 scrollbar-none px-2"
              style={{ scrollbarWidth: 'none' }}
              data={sortedPulls}
              computeItemKey={(_i, pr) => pr.number}
              itemContent={(_i, pr) => (
                <div className="pb-2">
                  {row(pr, (
                    <button
                      onClick={() => setSelectedPull(pr.number)}
                      className={cardClass(selectedPull === pr.number)}
                    >
                      {cardInner(pr)}
                    </button>
                  ))}
                </div>
              )}
            />
          )
        )}
        <div className="pointer-events-none absolute bottom-0 left-0 right-0 h-8 bg-gradient-to-t from-bg to-transparent" />
      </div>

      <div className="flex-shrink-0 flex items-center gap-2 px-3 pt-2 pb-4 text-[12px] text-muted">
        <span title={
          prPersonFilterActive
            ? (prSearchTruncatedAt
              ? i18nT('apps.issueRadar.components.prList.resolved_by_github_search_capped', { n: prSearchTruncatedAt })
              : i18nT('apps.issueRadar.components.prList.resolved_by_github_search_across_the_whole_repo'))
            : prStateFilter !== 'open'
              ? i18nT('apps.issueRadar.components.prList.closed_merged_prs_are_capped_at_the_100_most_rec')
              : undefined
        }>
          {i18nT('apps.issueRadar.components.prList.pr', { count: filteredPulls.length })}
          {prSearchTruncatedAt ? '+' : ''}
        </span>
        {/* Cold-start: these are only the newest page (un-enriched) while the full
            list loads behind them. Say so, so the count does not read as the whole
            repo — the PR twin of IssueList's hint. */}
        {pullsPartial && (
          <span className="inline-flex items-center gap-1 text-muted opacity-70">
            <RefreshCw size={11} className="animate-spin" />
            {i18nT('apps.issueRadar.components.prList.loading_the_rest')}
          </span>
        )}
        <span className="ml-auto flex items-center gap-2">
          {lastUpdated && (
            <span className="tabular-nums" title={i18nT('apps.issueRadar.components.prList.time_since_the_pr_list_was_last_fetched_from_git')}>
              {i18nT('apps.issueRadar.components.prList.updated')} {lastUpdated}
            </span>
          )}
          <button
            onClick={refreshPulls}
            disabled={pullsRefreshing}
            title={i18nT('apps.issueRadar.components.prList.re_fetch_from', { label: terms.changeRequestPluralTitle, provider: terms.providerName })}
            aria-label={i18nT('apps.issueRadar.components.prList.refresh', { label: terms.changeRequestPlural })}
            className="inline-flex items-center cursor-pointer bg-transparent text-muted hover:text-text disabled:opacity-30"
          >
            <RefreshCw size={13} className={pullsRefreshing ? 'animate-spin' : ''} />
          </button>
        </span>
      </div>
    </section>
  )
}
