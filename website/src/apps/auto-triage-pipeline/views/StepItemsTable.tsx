// StepItemsTable — L1: the items sitting inside one pipeline step.
//
// One row per issue. Expanding a row shows what an operator actually asks of an
// item -- when it entered, when it last moved, and what its agent sessions have
// cost -- with the sessions table rendered INLINE, in the row it describes.
//
// The expanded row deliberately does NOT draw the item's own event trail. A strip
// of the pipeline's internal event names answers a question nobody at this level
// is asking, and it pushed the cost table (the reason to open a row at all) below
// the fold. Per-item relationships belong to a dependency view, which Issue Radar
// already owns; it is not this view's job.
//
// Type scale follows the dashboard's dense-table idiom -- 13px for the item title,
// 12px for values, 11px for labels and meta -- rather than the 10px it used to
// use, which was a size no other app in the product renders body text at.
//
// Two fields are deliberately shown as "not recorded" rather than as an absence
// of the thing itself:
//   * labels/assignees come from a local cache that is only written when a human
//     opens the issue elsewhere, so most pipeline items legitimately have none
//     cached. Rendering that as "no labels" would assert something the data does
//     not say.
//   * a pull request number is only shown when a structured field or a URL
//     recorded it. Some records name it only in prose, and the same prose also
//     mentions OTHER pull requests, so guessing produces a confidently wrong
//     link that an operator would click.
import type { ReactNode } from 'react'
import { ChevronRight, ExternalLink, GitPullRequest, RotateCcw } from 'lucide-react'
import { Badge, Card, EmptyState } from '../../../components/ui'
import { i18nT } from '../../../i18n/t'
import type { StepItem } from '../api'
import { EMPTY_PLACEHOLDER, formatRelativeTime } from '../lib/format'

const C = {
  card: 'var(--card)',
  border: 'var(--border)',
  text: 'var(--text)',
  dim: 'var(--text-dim)',
  accent: 'var(--accent)',
} as const

// One placeholder for "not recorded", imported from the formatter rather than
// re-declared. Two spellings shipped side by side ('--' here, an em dash from the
// formatter) read as two different concepts on the same table.
const NOT_RECORDED = EMPTY_PLACEHOLDER

/** How long an item has been waiting, from the most recent thing known about it. */
export function waitedSince(item: StepItem): number | null {
  return item.lastEventAt ?? item.dispatchedAt ?? item.queuedAt ?? null
}

/** True when this item has a session worth drilling into.
 *
 * Only the working steps open a session, so an item with no slot has no cost table
 * to show and must not be given an empty one -- an empty table looks like lost
 * data rather than like work that never started.
 */
export function hasSessions(item: StepItem): boolean {
  return Boolean(item.slot) || item.previousSlots.length > 0
}

/** One label/value pair in the expanded row's basics block. */
function Fact({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <dt className="text-[11px] uppercase tracking-wide" style={{ color: C.dim }}>
        {label}
      </dt>
      <dd
        className={`truncate text-[12px] ${mono ? 'font-mono' : ''}`}
        style={{ color: C.text }}
        title={value}
      >
        {value}
      </dd>
    </div>
  )
}

function ItemRow({
  item,
  expanded,
  onToggle,
  renderSessions,
  nowMs,
}: {
  item: StepItem
  expanded: boolean
  onToggle: (n: number) => void
  renderSessions: (n: number) => ReactNode
  nowMs: number
}) {
  const waited = waitedSince(item)
  const at = (ts: number | null) => (ts === null ? NOT_RECORDED : formatRelativeTime(ts, nowMs))
  return (
    <div
      className="rounded-lg border"
      style={{ background: C.card, borderColor: expanded ? C.accent : C.border }}
      data-testid={`atp-item-${item.number}`}
    >
      {/* Narrow-first: the row WRAPS on a small viewport and only becomes a single
          fixed-column line at the sm breakpoint. Fixed non-shrinking widths on a
          320px viewport pushed the controls past the row edge and forced horizontal
          overflow, which on a table of rows means the actions become unreachable.
          The toggle is a button over the chevron/number/title rather than over the
          whole row, because the row also carries a real link -- an anchor nested in
          a button is invalid, and its click would be eaten by the toggle. */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 p-2.5 sm:flex-nowrap">
        <button
          type="button"
          onClick={() => onToggle(item.number)}
          aria-expanded={expanded}
          aria-label={i18nT('apps.autoTriagePipeline.global.toggle_detail', {
            number: item.number,
          })}
          data-testid={`atp-toggle-${item.number}`}
          className="flex min-w-0 flex-1 items-center gap-2 rounded text-left"
        >
          <ChevronRight
            aria-hidden="true"
            className={`h-3.5 w-3.5 shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`}
            style={{ color: C.dim }}
          />

          <span
            className="shrink-0 font-mono text-[12px] tabular-nums sm:w-16"
            style={{ color: C.accent }}
          >
            #{item.number}
          </span>

          <span className="min-w-0 flex-1 truncate text-[13px]" style={{ color: C.text }}>
            {item.title || i18nT('apps.autoTriagePipeline.global.untitled')}
          </span>
        </button>

        {item.resumeCount > 0 ? (
          <span
            className="flex shrink-0 items-center gap-0.5 text-[11px] tabular-nums"
            style={{ color: C.dim }}
            title={i18nT('apps.autoTriagePipeline.global.resumed_times', {
              count: item.resumeCount,
            })}
          >
            <RotateCcw aria-hidden="true" className="h-3 w-3" />
            {item.resumeCount}
          </span>
        ) : null}

        {item.needsHuman ? (
          <Badge variant="warn">{i18nT('apps.autoTriagePipeline.global.needs_human')}</Badge>
        ) : null}

        <span
          className="shrink-0 text-[11px] tabular-nums sm:w-20 sm:text-right"
          style={{ color: C.dim }}
        >
          {waited === null ? NOT_RECORDED : formatRelativeTime(waited, nowMs)}
        </span>

        {item.pr ? (
          <a
            href={`https://github.com/kirodotdev/KiroCrew/pull/${item.pr}`}
            target="_blank"
            rel="noreferrer"
            className="flex shrink-0 items-center gap-1 text-[11px] tabular-nums sm:w-16"
            style={{ color: C.accent }}
          >
            <GitPullRequest aria-hidden="true" className="h-3 w-3" />
            {item.pr}
            <ExternalLink aria-hidden="true" className="h-2.5 w-2.5" />
          </a>
        ) : (
          <span
            className="shrink-0 text-[11px] sm:w-16"
            style={{ color: C.dim }}
            title={i18nT('apps.autoTriagePipeline.global.pr_not_recorded')}
          >
            {NOT_RECORDED}
          </span>
        )}
      </div>

      {expanded ? (
        <div className="border-t px-2.5 py-2.5" style={{ borderColor: C.border }}>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 md:grid-cols-4">
            <Fact
              label={i18nT('apps.autoTriagePipeline.global.queued')}
              value={at(item.queuedAt)}
            />
            <Fact
              label={i18nT('apps.autoTriagePipeline.global.dispatched')}
              value={at(item.dispatchedAt)}
            />
            <Fact
              label={i18nT('apps.autoTriagePipeline.global.last_event')}
              value={item.lastEvent || NOT_RECORDED}
              mono
            />
            <Fact
              label={i18nT('apps.autoTriagePipeline.global.author')}
              value={item.author || i18nT('apps.autoTriagePipeline.global.not_cached')}
            />
            <Fact
              label={i18nT('apps.autoTriagePipeline.global.labels')}
              value={
                item.labels.length > 0
                  ? item.labels.join(', ')
                  : i18nT('apps.autoTriagePipeline.global.not_cached')
              }
            />
            <Fact
              label={i18nT('apps.autoTriagePipeline.global.assignees')}
              value={
                item.assignees.length > 0
                  ? item.assignees.join(', ')
                  : i18nT('apps.autoTriagePipeline.global.not_cached')
              }
            />
            <Fact
              label={i18nT('apps.autoTriagePipeline.global.comments')}
              value={
                item.comments === null
                  ? i18nT('apps.autoTriagePipeline.global.not_cached')
                  : String(item.comments)
              }
            />
          </dl>

          {/* The cost table lives HERE, inside the row it describes, rather than in
              a panel appended after the whole list. Appended, it rendered as the
              page's last element below every other item, so "open the sessions of
              the first row" put the answer twenty rows further down and could not
              be scrolled into the middle of the viewport. */}
          <div className="mt-3 border-t pt-2.5" style={{ borderColor: C.border }}>
            {hasSessions(item) ? (
              renderSessions(item.number)
            ) : (
              <p className="text-[11px]" style={{ color: C.dim }}>
                {i18nT('apps.autoTriagePipeline.global.sessions_empty_title')}
              </p>
            )}
          </div>
        </div>
      ) : null}
    </div>
  )
}

export default function StepItemsTable({
  stepKey,
  items,
  expandedItem,
  onToggleItem,
  renderSessions,
  nowMs,
}: {
  stepKey: string
  items: StepItem[]
  expandedItem: number | null
  onToggleItem: (n: number) => void
  renderSessions: (n: number) => ReactNode
  nowMs: number
}) {
  if (items.length === 0) {
    return (
      <Card className="p-4">
        <EmptyState
          icon={<GitPullRequest aria-hidden="true" className="h-5 w-5" />}
          title={i18nT('apps.autoTriagePipeline.global.step_empty_title')}
          subtitle={i18nT('apps.autoTriagePipeline.global.step_empty_subtitle')}
          testId="atp-step-empty"
        />
      </Card>
    )
  }

  return (
    <div className="flex flex-col gap-1.5" data-testid={`atp-items-${stepKey}`}>
      {items.map((item) => (
        <ItemRow
          key={item.number}
          item={item}
          expanded={expandedItem === item.number}
          onToggle={onToggleItem}
          renderSessions={renderSessions}
          nowMs={nowMs}
        />
      ))}
    </div>
  )
}
