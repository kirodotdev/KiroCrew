// The live-status display for a single run (review thread).
//
// While a run works this renders: a run-level progress bar (how many changes
// have reached a terminal phase, over the total), a clock that ticks up while
// the run is running (and freezes at the final elapsed once it finishes), a
// per-change row list with a phase icon + label each, and — only while running —
// a Cancel button.
//
// Cancellation is COOPERATIVE on the backend: it drops the still-queued PRs but
// a review already in flight runs to completion. The button copy says so; it
// must never promise an instant stop.
import { useEffect, useState } from 'react'
import {
  CheckCircle2, CircleSlash, Clock, Loader2, XCircle, XOctagon, type LucideIcon,
} from 'lucide-react'
import type { ChangeActivity, PoolStats, Run, RunProgressEntry } from '../lib/types'
import { TERMINAL_PHASES, formatElapsed, phaseLabel, prLabelFromChange, runElapsedMs } from '../lib/format'

import { i18nT } from '../../../i18n/t'
/** Icon + colour for a per-change phase. Static class strings so Tailwind keeps
 * them; the reviewing spinner respects motion-reduce. */
function phaseVisual(phase: string): { Icon: LucideIcon; color: string; spin?: boolean } {
  switch (phase) {
    case 'reviewing': return { Icon: Loader2, color: 'text-accent', spin: true }
    case 'done': return { Icon: CheckCircle2, color: 'text-ok' }
    case 'failed': return { Icon: XCircle, color: 'text-danger' }
    case 'cancelled': return { Icon: CircleSlash, color: 'text-muted' }
    default: return { Icon: Clock, color: 'text-muted' }
  }
}

/** One change's live row: its parsed identity, a phase icon, and the phase
 * label. */
function ChangeRow({ change, entry }: { change: string; entry?: RunProgressEntry }) {
  const phase = entry?.phase ?? 'queued'
  const { Icon, color, spin } = phaseVisual(phase)
  return (
    <li className="flex items-center justify-between gap-2 py-1 text-[12px]">
      <span className="min-w-0 truncate text-text">{prLabelFromChange(change) || change}</span>
      <span className={`inline-flex flex-shrink-0 items-center gap-1.5 ${color}`}>
        <Icon size={13} className={spin ? 'animate-spin motion-reduce:animate-none' : ''} aria-hidden="true" />
        {phaseLabel(phase)}
      </span>
    </li>
  )
}

export default function RunProgress({
  run, pool, onCancel, cancelling = false, typicalMs = null,
}: {
  run: Run
  pool?: PoolStats | null
  onCancel?: () => void
  cancelling?: boolean
  /** Median duration of past runs of this size, from the caller's run list. */
  typicalMs?: number | null
}) {
  const running = run.status === 'running'

  // Tick once a second while running so the elapsed clock advances; the interval
  // is torn down on unmount and whenever the run stops running.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!running) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [running])

  const total = run.changes.length
  // Counted separately: "1 / 1 reviewed" for a change that FAILED was a lie the
  // empty report then contradicted. The bar still tracks all terminal phases —
  // it measures work finished, not work succeeded.
  const tally = run.changes.reduce((acc, change, i) => {
    const cid = run.change_ids?.[i] ?? change
    const phase = run.progress?.[cid]?.phase
    if (!phase || !TERMINAL_PHASES.has(phase)) return acc
    if (phase === 'done') return { ...acc, done: acc.done + 1, terminal: acc.terminal + 1 }
    if (phase === 'failed') return { ...acc, failed: acc.failed + 1, terminal: acc.terminal + 1 }
    return { ...acc, cancelled: acc.cancelled + 1, terminal: acc.terminal + 1 }
  }, { done: 0, failed: 0, cancelled: 0, terminal: 0 })
  const pct = total > 0 ? Math.round((tally.terminal / total) * 100) : 0
  // A full bar in the accent colour reads as success, so it must not be what a
  // wholly-failed run shows. Class strings are static so Tailwind keeps them.
  const barFill = !running && tally.failed > 0 && tally.done === 0
    ? 'bg-danger'
    : 'bg-accent'

  // The newest activity across whatever is in flight. A single-PR run spends its
  // whole life at 0%, so this is the only thing that shows it is alive.
  const activity = run.changes.reduce<ChangeActivity | null>((best, change, i) => {
    const cid = run.change_ids?.[i] ?? change
    const entry = run.progress?.[cid]
    if (entry?.phase !== 'reviewing' || !entry.activity) return best
    return !best || entry.activity.step > best.step ? entry.activity : best
  }, null)
  const elapsed = formatElapsed(runElapsedMs(run.started_at, run.finished_at, now))
  const cancelRequested = Boolean(run.cancel_requested_at) || cancelling

  return (
    <div className="flex flex-col gap-3">
      {/* Header: completion tally on the left, the elapsed clock on the right. */}
      <div className="flex items-center justify-between gap-2 text-[12px] text-muted">
        <span className="tabular-nums">
          {tally.done} / {total} {i18nT('apps.codeReviewSage.components.runProgress.reviewed')}
          {tally.failed > 0 && (
            <span className="text-danger"> · {tally.failed} {i18nT('apps.codeReviewSage.components.runProgress.failed')}</span>
          )}
          {tally.cancelled > 0 && (
            <span> · {tally.cancelled} {i18nT('apps.codeReviewSage.components.runProgress.cancelled')}</span>
          )}
        </span>
        <span className="inline-flex items-center gap-1.5 tabular-nums" title={i18nT('apps.codeReviewSage.components.runProgress.elapsed_time')}>
          <Clock size={12} aria-hidden="true" />
          {elapsed}
        </span>
      </div>

      {/* Run-level progress bar. While a run is genuinely at 0% the bar would be
          an empty trough for minutes, which reads as stalled — so it shows an
          indeterminate sweep instead of a fake percentage. */}
      <div
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={i18nT('apps.codeReviewSage.components.runProgress.review_progress')}
        className="h-1.5 w-full overflow-hidden rounded-full bg-bg-elevated"
      >
        {running && pct === 0 ? (
          <div className="h-full w-1/3 rounded-full bg-accent/70 animate-sage-sweep motion-reduce:animate-none motion-reduce:w-full motion-reduce:bg-accent/40" />
        ) : (
          <div
            className={`h-full rounded-full ${barFill} transition-[width] duration-500 motion-reduce:transition-none`}
            style={{ width: `${pct}%` }}
          />
        )}
      </div>

      {/* What the reviewer is doing, and roughly how long this usually takes.
          Neither is a percentage — together they answer "is it stuck?" and "how
          much longer?", which is what an opaque single-turn review cannot. */}
      {running && (activity || typicalMs) && (
        <div className="flex items-center justify-between gap-2 text-[11px] text-muted">
          <span className="inline-flex items-center gap-1.5 min-w-0">
            {activity && (
              <>
                <Loader2
                  size={11}
                  className="flex-shrink-0 animate-spin motion-reduce:animate-none"
                  aria-hidden="true"
                />
                <span className="truncate">
                  {activity.tool || i18nT('apps.codeReviewSage.components.runProgress.working')}
                  <span className="tabular-nums"> {i18nT('apps.codeReviewSage.components.runProgress.step')} {activity.step}</span>
                </span>
              </>
            )}
          </span>
          {typicalMs && (
            <span className="flex-shrink-0 tabular-nums" title={i18nT('apps.codeReviewSage.components.runProgress.median_of_past_runs_of_this_size')}>
              {i18nT('apps.codeReviewSage.components.runProgress.usually')}{formatElapsed(typicalMs)}
            </span>
          )}
        </div>
      )}

      {/* Pool utilisation only when it TELLS you something: with a single PR in
          flight "1 of 5 reviewers busy" restates the progress bar. It earns its
          line when the run has several PRs, or when other runs are competing for
          the same workers. */}
      {pool && typeof pool.busy === 'number' && typeof pool.max === 'number'
        && (total > 1 || pool.busy > 1) && (
        <div className="text-[11px] text-muted">
          {i18nT('apps.codeReviewSage.components.runProgress.reviewers_busy',
            { busy: pool.busy, max: pool.max })}
        </div>
      )}

      {/* Per-change rows — only when there is more than one change. For a single
          PR the row repeats the tally and the bar directly above it, and the PR
          it names is the one already in the pane's header. */}
      {total > 1 && (
        <ul className="flex flex-col divide-y divide-border">
          {run.changes.map((change, i) => {
            const cid = run.change_ids?.[i] ?? change
            return <ChangeRow key={cid} change={change} entry={run.progress?.[cid]} />
          })}
        </ul>
      )}

      {/* Cancel — only while running. Cooperative: the copy is explicit that a
          review already underway finishes. The hint is VISIBLE text rather than
          a tooltip repeat, so it is stated exactly once. */}
      {running && onCancel && (
        <div className="flex flex-col gap-1">
          <button
            type="button"
            onClick={onCancel}
            disabled={cancelRequested}
            className={
              'inline-flex items-center justify-center gap-1.5 rounded-lg border border-border '
              + 'bg-card px-3 py-1.5 text-[12px] font-medium text-text transition-colors '
              + 'hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-50 '
              + 'focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 cursor-pointer'
            }
          >
            <XOctagon size={13} aria-hidden="true" />
            {cancelRequested
              ? i18nT('apps.codeReviewSage.components.runProgress.cancelling')
              : i18nT('apps.codeReviewSage.components.runProgress.cancel_review')}
          </button>
          <span className="text-[11px] text-muted">
            {i18nT('apps.codeReviewSage.components.runProgress.stops_queued_prs_one_already_being_reviewed_will')}
          </span>
        </div>
      )}
    </div>
  )
}
