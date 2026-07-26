import { useEffect, useState } from 'react'
import { motion, AnimatePresence, useReducedMotion } from 'framer-motion'
import { RefreshCw, Search, X } from 'lucide-react'
import { useIssueRadar } from '../context'
import { relativeTimeOrDate, relativeTime } from '../lib/format'
import type { Issue } from '../api'
import LabelChip from './LabelChip'
import ListSkeleton from './ListSkeleton'
import ListEmptyState from './ListEmptyState'

/** Above this many rendered rows we skip the per-card layout/enter animation:
 * Framer's layout pass measures every node, which janks on large repos (Kiro
 * has thousands of open issues and the list isn't virtualized yet). Typing a
 * search that narrows the list back under the cap re-enables the animation. */
const ANIM_CAP = 200

/** Middle column: a search box, the filtered + sorted issue list (cards
 * animate as the search narrows them), and a footer carrying the count, the
 * time since the last refresh, and the refresh button. */
export default function IssueList() {
  const {
    filteredIssues, sortedIssues, issuesLoading, issuesError,
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
            placeholder="Search Issues…"
            aria-label="Search issues"
            className="flex-1 min-w-0 bg-transparent py-2.5 text-[13px] text-text placeholder:text-muted outline-none"
          />
          {query && (
            <button
              onClick={() => setQuery('')}
              title="Clear search"
              aria-label="Clear search"
              className="flex-shrink-0 cursor-pointer bg-transparent leading-none text-muted hover:text-text"
            >
              <X size={13} />
            </button>
          )}
        </div>
      </div>

      {/* Card list — a bottom gradient fades the last cards out (replaces the
          old hard divider line above the footer). */}
      <div className="relative flex-1 min-h-0">
        <div className="absolute inset-0 overflow-y-auto scrollbar-none px-2 pb-2 flex flex-col gap-2" style={{ scrollbarWidth: 'none' }}>
          {issuesLoading && <ListSkeleton />}
          {issuesError && <div className="px-1 py-2 text-[14px] text-danger">{issuesError.message}</div>}
          {!issuesLoading && filteredIssues.length === 0 && (
            <ListEmptyState searching={Boolean(query.trim())} label="Issues" />
          )}
          {animate ? (
            <AnimatePresence initial={false} mode="popLayout">
              {sortedIssues.map((iss) => (
                <motion.button
                  key={iss.number}
                  layout
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
          ) : (
            sortedIssues.map((iss) => (
              <button
                key={iss.number}
                onClick={() => setSelectedIssue(iss.number)}
                className={cardClass(selectedIssue === iss.number)}
              >
                {cardInner(iss)}
              </button>
            ))
          )}
        </div>
        {/* Bottom fade — the last cards dissolve toward the footer instead of a
            hard divider. Fades to --bg (the panel background behind the list). */}
        <div className="pointer-events-none absolute bottom-0 left-0 right-0 h-8 bg-gradient-to-t from-bg to-transparent" />
      </div>

      {/* Footer — count on the left, last-refresh time + refresh on the right.
          The gradient fade above (see card list) replaces the old top border. */}
      <div className="flex-shrink-0 flex items-center gap-2 px-3 pt-2 pb-4 text-[12px] text-muted">
        <span title={stateFilter === 'closed' && issues.length >= 100 ? 'Closed issues are capped at the 100 most recently updated' : undefined}>
          {filteredIssues.length} issue{filteredIssues.length === 1 ? '' : 's'}
        </span>
        <span className="ml-auto flex items-center gap-2">
          {lastUpdated && (
            <span className="tabular-nums" title="Time since the issue list was last fetched from GitHub">
              Updated {lastUpdated}
            </span>
          )}
          <button
            onClick={refresh}
            disabled={refreshing}
            title="Re-fetch issues + labels from GitHub"
            aria-label="Refresh issues"
            className="inline-flex items-center cursor-pointer bg-transparent text-muted hover:text-text disabled:opacity-30"
          >
            <RefreshCw size={13} className={refreshing ? 'animate-spin' : ''} />
          </button>
        </span>
      </div>
    </section>
  )
}
