// One row in the run (thread) list.
//
// Anatomy mirrors Issue Radar's PR card: a `<button>` (the whole card is the
// click target) carrying the selected-state border. Two rows inside:
//   • identity + status pill — the repo ("owner/name") for a repo-wide run, else
//     the first change's parsed "owner/repo#123" plus a "+N more" tail;
//   • relative age + (for finished runs) the red / yellow band counts read from
//     the folded report summary.
import { GitPullRequest, Trash2 } from 'lucide-react'
import type { Run } from '../lib/types'
import { useState } from 'react'

import { effectiveRunStatus, failureReason, relativeAge, runIdentity } from '../lib/format'
import RunStatusPill from './RunStatusPill'

import { i18nT } from '../../../i18n/t'
/** Selected-state card shell, matching PrList: accent border when selected,
 * else the resting border. */
function cardClass(selected: boolean): string {
  return (
    'w-full text-left rounded-lg border p-2.5 cursor-pointer bg-card hover:bg-bg-hover '
    + 'transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 '
    + (selected ? 'border-accent' : 'border-border')
  )
}

/** The red / yellow counts a finished run folded into its summary. Zero-count
 * bands are omitted so a clean run stays quiet; nothing renders when the summary
 * has no report yet. */
function BandCounts({ run }: { run: Run }) {
  const bands = run.summary?.report?.bands
  if (!bands) return null
  const { red, yellow } = bands
  if (!red && !yellow) {
    return <span className="text-[11px] text-ok">{i18nT('apps.codeReviewSage.components.runCard.clean')}</span>
  }
  return (
    // The hover-revealed delete control sits in this row's bottom-right corner (the
    // top-right belongs to the status pill), so leave it room rather than letting the
    // trash icon cover the very count the cursor came to read.
    <span className="inline-flex items-center gap-2 text-[11px] tabular-nums transition-[padding] group-hover:pr-6">
      {red > 0 && (
        <span className="inline-flex items-center gap-1 text-danger" title={i18nT('apps.codeReviewSage.components.runCard.needs_review_count', { count: red })}>
          <span className="h-1.5 w-1.5 rounded-full bg-danger" aria-hidden="true" />
          {red}
        </span>
      )}
      {yellow > 0 && (
        <span className="inline-flex items-center gap-1 text-warn" title={i18nT('apps.codeReviewSage.components.runCard.worth_a_glance_count', { count: yellow })}>
          <span className="h-1.5 w-1.5 rounded-full bg-warn" aria-hidden="true" />
          {yellow}
        </span>
      )}
    </span>
  )
}

export default function RunCard({
  run, selected, onSelect, onDelete, deleting = false,
}: {
  run: Run
  selected: boolean
  onSelect: () => void
  /** Omitted where deletion does not belong (e.g. a read-only listing). */
  onDelete?: () => void
  deleting?: boolean
}) {
  const { label, more } = runIdentity(run.repo, run.changes)
  // A finished run dates from finished_at; a live one from started_at.
  const finished = Boolean(run.finished_at)
  const age = relativeAge(run.finished_at || run.started_at)
  const failure = failureReason(run)

  // Deleting a review destroys its report and per-run data, and nothing here can
  // bring it back, so the trash asks first. The confirm replaces the card's own
  // metadata row rather than opening a dialog: the card IS the thing being
  // deleted, so the question belongs on it.
  const [confirming, setConfirming] = useState(false)

  return (
    <div className="group relative">
    <button
      type="button"
      onClick={onSelect}
      aria-current={selected}
      // Without this the accessible name is the card's whole text content run
      // together ("acme/widgets Done 2m ago 1 2"), which is also ambiguous
      // against the rail's row for the same repo.
      aria-label={more > 0
        ? i18nT('apps.codeReviewSage.components.runCard.review_of_with_more',
          { label: label || i18nT('apps.codeReviewSage.components.runCard.pull_requests'), count: more })
        : i18nT('apps.codeReviewSage.components.runCard.review_of',
          { label: label || i18nT('apps.codeReviewSage.components.runCard.pull_requests') })}
      className={cardClass(selected)}
    >
      <div className="flex items-center justify-between gap-2 mb-1">
        <span className="inline-flex min-w-0 items-center gap-1.5 text-[13px] font-medium text-text">
          <GitPullRequest size={13} className="flex-shrink-0 text-muted" aria-hidden="true" />
          <span className="truncate">{label || i18nT('apps.codeReviewSage.components.runCard.review')}</span>
          {more > 0 && <span className="flex-shrink-0 text-muted">+{more} {i18nT('apps.codeReviewSage.components.runCard.more')}</span>}
        </span>
        {/* Derived, not stored: a run whose every change failed must not read
            "Done" next to a tally that says nothing was reviewed. */}
        <RunStatusPill
          status={effectiveRunStatus(run)}
          cancelRequested={Boolean(run.cancel_requested_at)}
        />
      </div>
      <div className="flex items-center justify-between gap-2 text-[12px] text-muted">
        <span className="tabular-nums">{age}</span>
        {finished && <BandCounts run={run} />}
      </div>
      {/* A failed card said only "Error"; the cause is already on the run, and
          knowing it is a restart rather than a bad pull request changes what you
          do next. */}
      {failure && (
        <div
          className="mt-1 text-[11.5px] text-danger leading-[1.4] line-clamp-2"
          title={failure.raw || failure.text}
        >
          {failure.text}
        </div>
      )}
    </button>

    {/* Sibling of the card, not a child: a button inside a button is invalid and
        the click would select the review it is meant to delete. */}
    {onDelete && !confirming && (
      <button
        type="button"
        onClick={() => setConfirming(true)}
        aria-label={i18nT('apps.codeReviewSage.components.runCard.delete_review_of',
          { label: label || i18nT('apps.codeReviewSage.components.runCard.pull_requests') })}
        title={i18nT('apps.codeReviewSage.components.runCard.delete_this_review')}
        // BOTTOM-right, not top-right: the status pill lives in the top-right corner,
        // and a destructive control that appears on hover exactly where the eye reads
        // "Done" both hides the state being checked and puts delete under the cursor
        // that was only inspecting. This is also where the confirm bar appears, so the
        // control and its confirmation occupy one place.
        className="absolute bottom-1.5 right-1.5 rounded-md bg-card/90 p-1 text-muted opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100 hover:text-danger cursor-pointer"
      >
        <Trash2 size={12} />
      </button>
    )}
    {onDelete && confirming && (
      <div className="absolute inset-x-1.5 bottom-1.5 flex items-center gap-1.5 rounded-md border border-danger bg-bg-elevated px-2 py-1 text-[11.5px]">
        <span className="flex-1 text-text">{i18nT('apps.codeReviewSage.components.runCard.delete_this_review_2')}</span>
        <button
          type="button"
          onClick={() => { setConfirming(false); onDelete() }}
          disabled={deleting}
          className="rounded bg-transparent px-1 text-danger font-medium hover:underline disabled:opacity-50 cursor-pointer"
        >
          {i18nT('apps.codeReviewSage.components.runCard.delete')}
        </button>
        <button
          type="button"
          onClick={() => setConfirming(false)}
          className="rounded bg-transparent px-1 text-muted hover:text-text cursor-pointer"
        >
          {i18nT('apps.codeReviewSage.components.runCard.cancel')}
        </button>
      </div>
    )}
    </div>
  )
}
