import { useEffect, useState } from 'react'
import { motion, AnimatePresence, useReducedMotion } from 'framer-motion'
import { Virtuoso } from 'react-virtuoso'
import { RefreshCw, Search, X } from 'lucide-react'
import { useIssueRadar } from '../context'
import { relativeTimeOrDate, relativeTime } from '../lib/format'
import type { Issue } from '../api'
import LabelChip from './LabelChip'
import ListSkeleton from './ListSkeleton'
import ListEmptyState from './ListEmptyState'

import { i18nT } from '../../../i18n/t'
/** Above this many rendered rows the per-card layout/enter animation is dropped
 * AND the list switches to a virtualized scroller: Framer's layout pass measures
 * every node, and mounting thousands of card DOM nodes at once janks on large
 * repos (Kiro has thousands of open issues). Under the cap the list is a plain
 * flow of animated cards — cheap, and the reorder/enter animation is worth it;
 * over it, only the visible rows exist and the animation would fight the
 * virtualizer anyway. Typing a search that narrows the list back under the cap
 * re-enables both. */
const ANIM_CAP = 200

/** Middle column: a search box, the filtered + sorted issue list (cards
 * animate as the search narrows them), and a footer carrying the count, the
 * time since the last refresh, and the refresh button.
 *
 * `resizing` is true while the user drags the column's width handle: card layout
 * animation is switched off for the duration, since animating a size change
 * scale-transforms the card and visibly stretches its text on every pointer
 * move. Dropped from the drag, the cards simply re-wrap. */
export default function IssueList({ resizing = false }: { resizing?: boolean }) {
  const {
    filteredIssues, sortedIssues, issuesLoading, issuesError, issuesPartial,
    stateFilter, issues, colorByName,
    selectedIssue, setSelectedIssue, refresh, refreshing,
    query, setQuery, issuesUpdatedAt,
  } = useIssueRadar()

  const reduce = useReducedMotion()
  const animate = !reduce && sortedIssues.length <= ANIM_CAP

  // Re-render every 30s so the "Updated Nm ago" label stays fresh without a
  // refetch.
  const [, tick] = useState(0)
  useEffect(() => {
    const id = setInterval(() => tick((t) => t + 1), 30_000)
    return () => clearInterval(id)
  }, [])

  const cardClass = (isSel: boolean) =>
    `w-full text-left rounded-lg border p-2.5 cursor-pointer bg-card hover:bg-bg-hover transition-colors ${
      isSel ? 'border-accent' : 'border-border'
    }`

  const cardInner = (iss: Issue) => (
    <>
      <div className="flex items-center justify-between gap-2 text-[12px] text-muted mb-1">
        <span className="truncate">
          <span className="font-bold text-accent">#{iss.number}</span>
          {iss.author ? ` · ${iss.author}` : ''}
        </span>
        <span className="flex-shrink-0">{relativeTimeOrDate(iss.updated_at)}</span>
      </div>
      <div className="text-[14px] leading-snug text-text line-clamp-2">{iss.title}</div>
      {iss.labels.length > 0 && (
        <div className="flex items-center gap-1.5 mt-1.5 flex-wrap">
          {iss.labels.map((name) => (
            <LabelChip key={name} name={name} color={colorByName.get(name) ?? '888888'} small />
          ))}
        </div>
      )}
    </>
  )

  const lastUpdated = relativeTime(issuesUpdatedAt)

  return (
    <section className="flex flex-col min-h-0 h-full">
      {/* Search box: bordered pill, leading glyph,
          transparent input, inline clear button. */}
      <div className="px-2 pt-2 pb-1.5 flex-shrink-0">
        <div className="flex items-center gap-1.5 rounded-lg border border-border bg-card px-2.5 transition-colors focus-within:border-accent">
          <Search size={14} className="flex-shrink-0 text-muted opacity-60" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={i18nT('apps.issueRadar.components.issueList.search_issues')}
            aria-label={i18nT('apps.issueRadar.components.issueList.search_issues_2')}
            className="flex-1 min-w-0 bg-transparent py-2.5 text-[13px] text-text placeholder:text-muted outline-none"
          />
          {query && (
            <button
              onClick={() => setQuery('')}
              title={i18nT('apps.issueRadar.components.issueList.clear_search')}
              aria-label={i18nT('apps.issueRadar.components.issueList.clear_search')}
              className="flex-shrink-0 cursor-pointer bg-transparent leading-none text-muted hover:text-text"
            >
              <X size={13} />
            </button>
          )}
        </div>
      </div>

      {/* Card list — a bottom gradient fades the last cards out. */}
      <div className="relative flex-1 min-h-0">
        {issuesLoading && (
          <div className="absolute inset-0 overflow-y-auto scrollbar-none px-2 pb-2 flex flex-col gap-2" style={{ scrollbarWidth: 'none' }}>
            <ListSkeleton />
          </div>
        )}
        {issuesError && <div className="px-3 py-2 text-[14px] text-danger">{issuesError.message}</div>}
        {!issuesLoading && filteredIssues.length === 0 && (
          <div className="px-2 pb-2">
            <ListEmptyState searching={Boolean(query.trim())} label={i18nT('apps.issueRadar.components.issueList.issues')} />
          </div>
        )}
        {!issuesLoading && sortedIssues.length > 0 && (
          animate ? (
            // Small list: a plain animated flow. Cheap, and the reorder/enter
            // animation is worth it. AnimatePresence needs all siblings mounted, so
            // this path is deliberately NOT virtualized (bounded by ANIM_CAP).
            <div className="absolute inset-0 overflow-y-auto scrollbar-none px-2 pb-2 flex flex-col gap-2" style={{ scrollbarWidth: 'none' }}>
              <AnimatePresence initial={false} mode="popLayout">
                {sortedIssues.map((iss) => (
                  <motion.button
                    key={iss.number}
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
                    onClick={() => setSelectedIssue(iss.number)}
                    className={cardClass(selectedIssue === iss.number)}
                  >
                    {cardInner(iss)}
                  </motion.button>
                ))}
              </AnimatePresence>
            </div>
          ) : (
            // Large list: virtualize so only the visible rows exist as DOM nodes,
            // instead of mounting thousands of cards on a big repo. Row gap is a
            // per-row bottom padding (Virtuoso lays rows out absolutely, so a flex
            // `gap` on the container does not apply).
            <Virtuoso
              className="absolute inset-0 scrollbar-none px-2"
              style={{ scrollbarWidth: 'none' }}
              data={sortedIssues}
              computeItemKey={(_i, iss) => iss.number}
              itemContent={(_i, iss) => (
                <div className="pb-2">
                  <button
                    onClick={() => setSelectedIssue(iss.number)}
                    className={cardClass(selectedIssue === iss.number)}
                  >
                    {cardInner(iss)}
                  </button>
                </div>
              )}
            />
          )
        )}
        {/* Bottom fade — the last cards dissolve toward the footer instead of a
            hard divider. Fades to --bg (the panel background behind the list). */}
        <div className="pointer-events-none absolute bottom-0 left-0 right-0 h-8 bg-gradient-to-t from-bg to-transparent" />
      </div>

      {/* Footer — count on the left, last-refresh time + refresh on the right. */}
      <div className="flex-shrink-0 flex items-center gap-2 px-3 pt-2 pb-4 text-[12px] text-muted">
        <span title={stateFilter === 'closed' && issues.length >= 100 ? i18nT('apps.issueRadar.components.issueList.closed_issues_are_capped_at_the_100_most_recentl') : undefined}>
          {i18nT('apps.issueRadar.components.issueList.issue', { count: filteredIssues.length })}
        </span>
        {/* Cold-start: these are only the newest page while the full list loads
            behind them. Say so, so the count does not read as the whole repo. */}
        {issuesPartial && (
          <span className="inline-flex items-center gap-1 text-muted opacity-70">
            <RefreshCw size={11} className="animate-spin" />
            {i18nT('apps.issueRadar.components.issueList.loading_the_rest')}
          </span>
        )}
        <span className="ml-auto flex items-center gap-2">
          {lastUpdated && (
            <span className="tabular-nums" title={i18nT('apps.issueRadar.components.issueList.time_since_the_issue_list_was_last_fetched_from')}>
              {i18nT('apps.issueRadar.components.issueList.updated')} {lastUpdated}
            </span>
          )}
          <button
            onClick={refresh}
            disabled={refreshing}
            title={i18nT('apps.issueRadar.components.issueList.re_fetch_issues_labels_from_github')}
            aria-label={i18nT('apps.issueRadar.components.issueList.refresh_issues')}
            className="inline-flex items-center cursor-pointer bg-transparent text-muted hover:text-text disabled:opacity-30"
          >
            <RefreshCw size={13} className={refreshing ? 'animate-spin' : ''} />
          </button>
        </span>
      </div>
    </section>
  )
}
