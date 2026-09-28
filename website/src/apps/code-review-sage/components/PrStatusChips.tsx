// The pull request's own status: is it open, can it merge, are its checks green.
//
// Sage's header carried the PR's identity (number, author, head) and the REVIEW's
// status, but nothing about the pull request itself — so a merged or conflicting
// PR looked identical to a healthy open one, and you had to open the Checks tab to
// learn 2 of 43 were failing. Every field here already arrives in the provider
// payload the tabs share; none of it costs an extra request.
import { AlertTriangle, CheckCircle2, Clock, GitMerge, GitPullRequest, XCircle } from 'lucide-react'

import type { PullRequestSource } from '../../../types'

import { i18nT } from '../../../i18n/t'
/** Chip treatments are static class strings so Tailwind's content scan keeps them. */
const CHIP = 'inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded-full border'

/** Open / merged / closed / draft. A draft is still "open" to the provider, so it
 *  is reported separately rather than replacing the state. */
function StateChip({ src }: { src: PullRequestSource }) {
  if (src.mergedAt) {
    return (
      <span className={`${CHIP} border-accent text-accent bg-accent-subtle`}>
        <GitMerge size={10} aria-hidden="true" /> {i18nT('apps.codeReviewSage.components.prStatusChips.merged')}
      </span>
    )
  }
  const state = (src.state || '').toLowerCase()
  if (state === 'closed') {
    return (
      <span className={`${CHIP} border-danger text-danger`}>
        <XCircle size={10} aria-hidden="true" /> {i18nT('apps.codeReviewSage.components.prStatusChips.closed')}
      </span>
    )
  }
  return (
    <span className={`${CHIP} border-ok text-ok bg-ok-subtle`}>
      <GitPullRequest size={10} aria-hidden="true" /> {i18nT('apps.codeReviewSage.components.prStatusChips.open')}
    </span>
  )
}

/** Only shown when it is actionable. "Mergeable" on an open PR is the expected
 *  case and saying so on every one would be noise; a conflict is not. */
function MergeChip({ src }: { src: PullRequestSource }) {
  if (src.mergedAt) return null
  const state = (src.mergeable || '').toLowerCase()
  if (state === 'conflicting') {
    return (
      <span className={`${CHIP} border-danger text-danger`}>
        <AlertTriangle size={10} aria-hidden="true" /> {i18nT('apps.codeReviewSage.components.prStatusChips.conflicts')}
      </span>
    )
  }
  if ((src.mergeStateStatus || '').toUpperCase() === 'BLOCKED') {
    return (
      <span className={`${CHIP} border-warn text-warn`}>
        <AlertTriangle size={10} aria-hidden="true" /> {i18nT('apps.codeReviewSage.components.prStatusChips.merge_blocked')}
      </span>
    )
  }
  return null
}

/** The check rollup, worst-first. Failing counts beat pending counts because a
 *  failure is the thing that stops the PR. */
function ChecksChip({ src }: { src: PullRequestSource }) {
  const checks = src.checks ?? []
  if (!checks.length) return null
  const failed = checks.filter((c) => c.bucket === 'failed').length
  const pending = checks.filter((c) => c.bucket === 'pending').length
  if (failed > 0) {
    return (
      <span className={`${CHIP} border-danger text-danger`} title={i18nT('apps.codeReviewSage.components.prStatusChips.checks_failing', { failed, count: checks.length })}>
        <XCircle size={10} aria-hidden="true" />
        <span className="tabular-nums">{failed} {i18nT('apps.codeReviewSage.components.prStatusChips.failing')}</span>
      </span>
    )
  }
  if (pending > 0) {
    return (
      <span className={`${CHIP} border-border text-muted`} title={i18nT('apps.codeReviewSage.components.prStatusChips.checks_running', { pending, count: checks.length })}>
        <Clock size={10} aria-hidden="true" />
        <span className="tabular-nums">{pending} {i18nT('apps.codeReviewSage.components.prStatusChips.running')}</span>
      </span>
    )
  }
  return (
    <span className={`${CHIP} border-ok text-ok`} title={i18nT('apps.codeReviewSage.components.prStatusChips.all_checks_passed', { count: checks.length })}>
      <CheckCircle2 size={10} aria-hidden="true" />
      <span className="tabular-nums">{checks.length} {i18nT('apps.codeReviewSage.components.prStatusChips.passed')}</span>
    </span>
  )
}

export default function PrStatusChips({ src }: { src: PullRequestSource | undefined }) {
  // Absent until the provider call lands. Rendering placeholders would imply a
  // status we do not know yet.
  if (!src) return null
  return (
    <>
      <StateChip src={src} />
      <MergeChip src={src} />
      <ChecksChip src={src} />
    </>
  )
}
