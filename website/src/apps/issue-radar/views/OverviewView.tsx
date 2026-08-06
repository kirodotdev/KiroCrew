import { useMemo, type ReactNode } from 'react'
import { RefreshCw, MessageSquare, ThumbsUp } from 'lucide-react'
import { relativeDate, readableText } from '../lib/format'
import { useIssueRadar } from '../context'
import type { Issue } from '../api'

import { i18nT } from '../../../i18n/t'
// Overview — the triage command center. A full-width bento of "what's true
// right now": headline KPIs, the issues most in need of action, label mix,
// backlog age, recent activity, and discussion hotspots. Overview stays a
// snapshot. Every number/row is a shortcut into the (filtered) issue list.
//
// All signals are derived client-side from data the backend already fetches
// (labels, comments, assignees, created/updated timestamps, thumbs-up reactions,
// author association) — no extra round-trips.

const DAY = 86400000

/** Whole days since an ISO timestamp (0 for missing/invalid). */
function ageDays(iso?: string): number {
  if (!iso) return 0
  const t = new Date(iso).getTime()
  if (isNaN(t)) return 0
  return Math.max(0, Math.floor((Date.now() - t) / DAY))
}

/** Epoch-ms for a created/updated timestamp (0 when missing) — for sorting. */
function ts(iso?: string): number {
  const t = new Date(iso ?? 0).getTime()
  return isNaN(t) ? 0 : t
}

export default function OverviewView() {
  const {
    issues, sortedRepoLabels, countByLabel,
    me, openIssues, setSelectedIssue, toggleLabel, toggleAssignedToMe,
    assignedToMe, refresh, refreshing, issuesLoading,
    needsTriage, isGoodFirstIssue,
  } = useIssueRadar()

  // ── headline signals + backlog-age histogram, one pass over the issues ──
  const stats = useMemo(() => {
    let untriaged = 0, unanswered = 0, unassigned = 0, stale = 0, fresh = 0, mine = 0
    const ageBuckets = [0, 0, 0, 0] // <1w · 1–4w · 1–6mo · >6mo
    for (const i of issues) {
      if (needsTriage(i)) untriaged++
      if (i.comments === 0) unanswered++
      if ((i.assignees ?? []).length === 0) unassigned++
      if (ageDays(i.updated_at) > 30) stale++
      const age = ageDays(i.created_at)
      if (age <= 7) fresh++
      if (me && (i.assignees ?? []).includes(me)) mine++
      if (age < 7) ageBuckets[0]++
      else if (age < 30) ageBuckets[1]++
      else if (age < 180) ageBuckets[2]++
      else ageBuckets[3]++
    }
    return { untriaged, unanswered, unassigned, stale, fresh, mine, ageBuckets }
  }, [issues, me, needsTriage])

  const open = issues.length
  const pct = (n: number) => (open ? Math.round((n / open) * 100) : 0)

  const tiles = [
    { key: 'open', label: i18nT('apps.issueRadar.views.overviewView.open_issues'), value: open, sub: '' },
    { key: 'untriaged', label: i18nT('apps.issueRadar.views.overviewView.untriaged'), value: stats.untriaged, sub: open ? `${pct(stats.untriaged)}% of open` : '' },
    { key: 'unanswered', label: i18nT('apps.issueRadar.views.overviewView.unanswered'), value: stats.unanswered, sub: i18nT('apps.issueRadar.views.overviewView.0_comments') },
    { key: 'unassigned', label: i18nT('apps.issueRadar.views.overviewView.unassigned'), value: stats.unassigned, sub: i18nT('apps.issueRadar.views.overviewView.no_owner') },
    { key: 'stale', label: i18nT('apps.issueRadar.views.overviewView.stale'), value: stats.stale, sub: i18nT('apps.issueRadar.views.overviewView.30d_idle') },
    { key: 'fresh', label: i18nT('apps.issueRadar.views.overviewView.new_this_week'), value: stats.fresh, sub: i18nT('apps.issueRadar.views.overviewView.last_7_days') },
  ]

  // ── needs attention: open issues with a clear triage gap (needs triage OR no
  // reply), newest first. Deterministic and legible — each card shows its age,
  // so the order is self-evident, and the tags say WHY it's here. ──
  const needsAttention = useMemo(
    () => issues
      .filter((i) => needsTriage(i) || i.comments === 0)
      .sort((a, b) => ts(b.created_at) - ts(a.created_at))
      .slice(0, 9),
    [issues, needsTriage],
  )

  const topLabels = useMemo(
    () => sortedRepoLabels
      .map((l) => ({ ...l, count: countByLabel.get(l.name) ?? 0 }))
      .filter((l) => l.count > 0)
      .slice(0, 8),
    [sortedRepoLabels, countByLabel],
  )
  const maxLabel = topLabels[0]?.count ?? 1

  const recentlyUpdated = useMemo(
    () => [...issues].sort((a, b) => ts(b.updated_at) - ts(a.updated_at)).slice(0, 6),
    [issues],
  )

  const hotspots = useMemo(
    () => [...issues]
      .sort((a, b) => (b.comments - a.comments) || ((b.thumbs_up ?? 0) - (a.thumbs_up ?? 0)))
      .slice(0, 6),
    [issues],
  )

  const openDetail = (n: number) => { setSelectedIssue(n); openIssues() }

  if (issuesLoading && open === 0) {
    return <div className="h-full flex items-center justify-center text-muted text-[14px]">{i18nT('apps.issueRadar.views.overviewView.loading_overview')}</div>
  }

  const maxAge = Math.max(1, ...stats.ageBuckets)
  const ageRows = [
    { label: i18nT('apps.issueRadar.views.overviewView.1_week'), n: stats.ageBuckets[0] },
    { label: i18nT('apps.issueRadar.views.overviewView.1_4_weeks'), n: stats.ageBuckets[1] },
    { label: i18nT('apps.issueRadar.views.overviewView.1_6_months'), n: stats.ageBuckets[2] },
    { label: i18nT('apps.issueRadar.views.overviewView.6_months'), n: stats.ageBuckets[3] },
  ]

  return (
    <div className="px-6 pt-4 pb-6 flex flex-col gap-4">
      {/* First row: your personal queue + a compact refresh, flush to the top. */}
      <div className="flex items-start gap-3">
        {me ? (
          <button
            onClick={toggleAssignedToMe}
            className={`flex-1 text-left rounded-xl border bg-bg-elevated shadow-sm p-4 flex items-center gap-4 cursor-pointer transition-colors ${
              assignedToMe ? 'border-accent' : 'border-border hover:border-border-strong'
            }`}
          >
            <div className="text-[26px] font-bold text-accent leading-none tabular-nums">{stats.mine}</div>
            <div>
              <div className="text-[13px] font-medium text-text">{i18nT('apps.issueRadar.views.overviewView.assigned_to_you')}</div>
              <div className="text-[11px] text-muted mt-0.5">{i18nT('apps.issueRadar.views.overviewView.filter_the_list_to_issues_assigned_to_you')}</div>
            </div>
          </button>
        ) : <div className="flex-1" />}
        <button
          onClick={refresh}
          disabled={refreshing}
          aria-label={i18nT('apps.issueRadar.views.overviewView.refresh_issues')}
          title={i18nT('apps.issueRadar.views.overviewView.re_fetch_issues_labels_from_github')}
          className="flex-shrink-0 inline-flex items-center justify-center h-9 w-9 rounded-lg border border-border text-muted hover:text-text hover:border-border-strong disabled:opacity-40 cursor-pointer"
        >
          <RefreshCw size={14} className={refreshing ? 'animate-spin' : ''} />
        </button>
      </div>

      {/* KPI strip — fills the width; each tile jumps into the issue list. */}
      <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-6 gap-3">
        {tiles.map((t) => (
          <button
            key={t.key}
            onClick={openIssues}
            className="text-left rounded-xl border border-border bg-bg-elevated shadow-sm p-4 hover:border-border-strong cursor-pointer transition-colors"
          >
            <div className="text-[26px] font-bold text-text leading-none">{t.value}</div>
            <div className="text-[12px] text-muted mt-1.5">{t.label}</div>
            {t.sub && <div className="text-[11px] text-muted opacity-60 mt-0.5">{t.sub}</div>}
          </button>
        ))}
      </div>

      {/* Needs attention — full width, filled by a multi-column triage grid so
       * there's no wasted whitespace. Newest untriaged/unanswered first. */}
      <Panel title={i18nT('apps.issueRadar.views.overviewView.needs_attention')} hint={i18nT('apps.issueRadar.views.overviewView.newest_issues_still_untriaged_or_unanswered')}>
        {needsAttention.length === 0 ? (
          <Empty>{i18nT('apps.issueRadar.views.overviewView.nothing_needs_attention')}</Empty>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-2.5">
            {needsAttention.map((i) => {
              const tags: string[] = []
              if (needsTriage(i)) tags.push(i18nT('apps.issueRadar.views.overviewView.untriaged_2'))
              if (i.comments === 0) tags.push(i18nT('apps.issueRadar.views.overviewView.no_reply'))
              if (isGoodFirstIssue(i)) tags.push(i18nT('apps.issueRadar.views.overviewView.good_first_issue'))
              if (i.author_association === 'FIRST_TIME_CONTRIBUTOR') tags.push(i18nT('apps.issueRadar.views.overviewView.first_timer'))
              return (
                <button
                  key={i.number}
                  onClick={() => openDetail(i.number)}
                  className="text-left rounded-lg border border-border bg-card hover:border-border-strong p-3 cursor-pointer transition-colors flex flex-col gap-1.5"
                >
                  <div className="flex items-center gap-1.5 text-[12px] text-muted">
                    <span className="font-bold text-accent flex-shrink-0">#{i.number}</span>
                    {i.author && <span className="truncate">{i.author}</span>}
                    <span className="ml-auto flex-shrink-0">{relativeDate(i.created_at ?? '')}</span>
                  </div>
                  <div className="text-[13px] leading-snug text-text line-clamp-2">{i.title}</div>
                  <div className="flex items-center gap-1.5 flex-wrap">
                    {tags.map((t) => (
                      <span key={t} className="text-[10px] px-1.5 py-0.5 rounded-full border border-border text-muted">{t}</span>
                    ))}
                    {(i.thumbs_up ?? 0) > 0 && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full border border-border text-muted inline-flex items-center gap-0.5">
                        <ThumbsUp size={9} /> {i.thumbs_up}
                      </span>
                    )}
                  </div>
                </button>
              )
            })}
          </div>
        )}
      </Panel>

      {/* Distributions: label mix + backlog age, side by side. */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
        <Panel title={i18nT('apps.issueRadar.views.overviewView.label_distribution')} className="h-80">
          {topLabels.length === 0 ? (
            <Empty>{i18nT('apps.issueRadar.views.overviewView.no_labels_in_use')}</Empty>
          ) : (
            <div className="flex-1 min-h-0 overflow-y-auto scrollbar-none" style={{ scrollbarWidth: 'none' }}>
              <div className="flex flex-col gap-2.5">
                {topLabels.map((l) => (
                  <button key={l.name} onClick={() => toggleLabel(l.name)} aria-label={i18nT('apps.issueRadar.views.overviewView.filter_by_label', { name: l.name })} className="text-left cursor-pointer">
                    <div className="flex items-center justify-between gap-2 mb-1">
                      <span
                        className="inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium max-w-full truncate"
                        style={{ backgroundColor: `#${l.color}`, color: readableText(l.color) }}
                      >
                        {l.name}
                      </span>
                      <span className="text-[11px] text-muted flex-shrink-0 tabular-nums">{l.count} · {pct(l.count)}%</span>
                    </div>
                    <div className="h-1.5 rounded-full bg-bg-hover overflow-hidden">
                      <div
                        className="h-full rounded-full"
                        style={{ width: `${(l.count / maxLabel) * 100}%`, backgroundColor: 'color-mix(in srgb, var(--muted) 45%, transparent)' }}
                      />
                    </div>
                  </button>
                ))}
              </div>
            </div>
          )}
        </Panel>

        <Panel title={i18nT('apps.issueRadar.views.overviewView.backlog_age')} hint={i18nT('apps.issueRadar.views.overviewView.by_creation_date')} className="h-80">
          <div className="flex flex-col gap-2.5">
            {ageRows.map((r) => (
              <div key={r.label}>
                <div className="flex items-center justify-between text-[11px] text-muted mb-1">
                  <span>{r.label}</span>
                  <span className="tabular-nums">{r.n} · {pct(r.n)}%</span>
                </div>
                <div className="h-1.5 rounded-full bg-bg-hover overflow-hidden">
                  <div className="h-full rounded-full" style={{ width: `${(r.n / maxAge) * 100}%`, backgroundColor: 'color-mix(in srgb, var(--muted) 45%, transparent)' }} />
                </div>
              </div>
            ))}
          </div>
        </Panel>
      </div>

      {/* Activity: recently updated + discussion hotspots, side by side. */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
        <Panel title={i18nT('apps.issueRadar.views.overviewView.recently_updated')}>
          {recentlyUpdated.length === 0 ? <Empty>{i18nT('apps.issueRadar.views.overviewView.no_activity')}</Empty> : (
            <div className="flex flex-col">
              {recentlyUpdated.map((i) => (
                <IssueRow
                  key={i.number}
                  iss={i}
                  onClick={() => openDetail(i.number)}
                  right={<span className="text-[11px] text-muted flex-shrink-0">{relativeDate(i.updated_at)}</span>}
                />
              ))}
            </div>
          )}
        </Panel>

        <Panel title={i18nT('apps.issueRadar.views.overviewView.discussion_hotspots')} hint={i18nT('apps.issueRadar.views.overviewView.most_comments_upvotes')}>
          {hotspots.length === 0 ? <Empty>{i18nT('apps.issueRadar.views.overviewView.no_discussion_yet')}</Empty> : (
            <div className="flex flex-col">
              {hotspots.map((i) => (
                <IssueRow
                  key={i.number}
                  iss={i}
                  onClick={() => openDetail(i.number)}
                  right={
                    <span className="flex items-center gap-2 text-[11px] text-muted flex-shrink-0">
                      <span className="inline-flex items-center gap-0.5"><MessageSquare size={11} /> {i.comments}</span>
                      {(i.thumbs_up ?? 0) > 0 && (
                        <span className="inline-flex items-center gap-0.5"><ThumbsUp size={11} /> {i.thumbs_up}</span>
                      )}
                    </span>
                  }
                />
              ))}
            </div>
          )}
        </Panel>
      </div>
    </div>
  )
}

// ── local presentational helpers (kept in-file so the view stays a single,
// independently-owned unit) ──

function Panel({ title, hint, className = '', children }: {
  title: string; hint?: string; className?: string; children: ReactNode
}) {
  return (
    <section className={`rounded-xl border border-border bg-bg-elevated shadow-sm p-4 flex flex-col ${className}`}>
      <div className="flex items-baseline justify-between gap-2 mb-3">
        <div className="text-[12px] font-semibold text-muted uppercase tracking-[.05em]">{title}</div>
        {hint && <div className="text-[11px] text-muted opacity-60 truncate">{hint}</div>}
      </div>
      {children}
    </section>
  )
}

function Empty({ children }: { children: ReactNode }) {
  return <div className="text-[13px] text-muted py-2">{children}</div>
}

/** One compact issue row for the activity list panels (Recently updated /
 * Discussion hotspots): #number · author, title, and a right-side metric. */
function IssueRow({ iss, onClick, right }: {
  iss: Issue; onClick: () => void; right?: ReactNode
}) {
  return (
    <button
      onClick={onClick}
      className="w-full text-left rounded-lg px-2 -mx-2 py-2 hover:bg-bg-hover cursor-pointer transition-colors flex items-start gap-2"
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5 text-[12px] mb-0.5">
          <span className="font-bold text-accent flex-shrink-0">#{iss.number}</span>
          {iss.author && <span className="text-muted truncate">{iss.author}</span>}
        </div>
        <div className="text-[13px] leading-snug text-text line-clamp-1">{iss.title}</div>
      </div>
      {right && <div className="pt-0.5 flex-shrink-0">{right}</div>}
    </button>
  )
}
