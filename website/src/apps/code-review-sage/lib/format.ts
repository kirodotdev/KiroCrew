// Pure, side-effect-free formatting helpers for Code Review Sage's thread list
// and live-status components. No React, no component imports — safe to pull into
// any module (and directly unit-testable).
//
// The labels themselves are catalog lookups: these helpers produce user-visible
// copy, so they are localized here rather than at each call site.
import { i18nT } from '../../../i18n/t'
import type { PullRequestLink } from '../../../utils/pullRequestLinks'
import type { RunStatus, PrRef, ChangePhase } from './types'

/** Compact "just now / 5m ago / 3h ago / 2d ago" from an ISO timestamp. Returns
 * '' for a falsy / unparseable input (e.g. a run that has no finished_at yet).
 * A future timestamp (clock skew) reads as "just now" rather than a negative. */
export function relativeAge(iso?: string): string {
  if (!iso) return ''
  const then = new Date(iso)
  if (isNaN(then.getTime())) return ''
  const secs = Math.floor((Date.now() - then.getTime()) / 1000)
  // Under a minute is "just now": at 45-59s the minute floor is 0, which rendered
  // "0m ago" -- a number that reads as a measurement while meaning "not yet one".
  if (secs < 60) return i18nT('apps.codeReviewSage.lib.format.just_now')
  const mins = Math.floor(secs / 60)
  if (mins < 60) return i18nT('apps.codeReviewSage.lib.format.minutes_ago', { count: mins })
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return i18nT('apps.codeReviewSage.lib.format.hours_ago', { count: hrs })
  const days = Math.floor(hrs / 24)
  return i18nT('apps.codeReviewSage.lib.format.days_ago', { count: days })
}

/** A running clock label from an elapsed millisecond span: "0:07", "3:42", or
 * "1:02:59" once it crosses an hour. Negative spans clamp to zero. */
export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const ss = String(s).padStart(2, '0')
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${ss}`
  return `${m}:${ss}`
}

/** Elapsed span (ms) for a run: start → finish, or start → now while it is still
 * running. `nowMs` is injectable so a ticking component drives re-renders and
 * tests stay deterministic. */
export function runElapsedMs(startedAt: string, finishedAt: string | undefined, nowMs: number): number {
  const start = new Date(startedAt).getTime()
  if (isNaN(start)) return 0
  const end = finishedAt ? new Date(finishedAt).getTime() : nowMs
  if (isNaN(end)) return 0
  return end - start
}

/** The per-change phases the driver reports as terminal — a change in one of
 * these will not advance further, so it counts toward run completion. */
export const TERMINAL_PHASES: ReadonlySet<string> = new Set<ChangePhase>(['done', 'failed', 'cancelled'])

/** Human label for a per-change phase. Unknown phases (the type allows an open
 * string) are title-cased rather than dropped. */
export function phaseLabel(phase: string): string {
  switch (phase) {
    case 'queued': return i18nT('apps.codeReviewSage.lib.format.phase_queued')
    case 'reviewing': return i18nT('apps.codeReviewSage.lib.format.phase_reviewing')
    case 'done': return i18nT('apps.codeReviewSage.lib.format.phase_done')
    case 'failed': return i18nT('apps.codeReviewSage.lib.format.phase_failed')
    case 'cancelled': return i18nT('apps.codeReviewSage.lib.format.phase_cancelled')
    default:
      return phase
        ? phase.charAt(0).toUpperCase() + phase.slice(1)
        : i18nT('apps.codeReviewSage.lib.format.phase_pending')
  }
}

/** Parse a compact "owner/repo#123" identity out of a change string. Handles
 * GitHub pull URLs (…/owner/repo/pull/123) and GitLab MR URLs
 * (…/owner/repo/-/merge_requests/123). Falls back to the URL path, then to the
 * raw string, so a picked link or a pasted id always renders as SOMETHING. */
export function prLabelFromChange(change: string): string {
  if (!change) return ''
  const m = change.match(/([^/]+)\/([^/]+)\/(?:-\/)?(?:pull|merge_requests)\/(\d+)/)
  if (m) return `${m[1]}/${m[2]}#${m[3]}`
  try {
    const path = new URL(change).pathname.replace(/^\/+|\/+$/g, '')
    if (path) return path
  } catch {
    /* not a URL — fall through */
  }
  return change
}

/** The run-list identity line: a repo-wide run shows "owner/name"; a PR run
 * shows the first change's parsed label plus a "+N more" tail when several PRs
 * are under review together. */
export function runIdentity(repo: string | undefined, changes: string[]): { label: string; more: number } {
  if (repo) return { label: repo, more: 0 }
  const first = prLabelFromChange(changes[0] ?? '')
  return { label: first, more: Math.max(0, changes.length - 1) }
}

/** Build a PrRef from a run's change URL + the driver's change id.
 *
 * A run records only the URL, so opening a review from the thread list has to
 * reconstruct enough of a PR to render the pane; the title/author/checks all
 * come from the provider fetch keyed on this URL. */
export function prRefFromChange(url: string, changeId: string): PrRef {
  const m = /\/(?:pull|merge_requests)\/(\d+)/.exec(url)
  return {
    url,
    number: m ? Number(m[1]) : 0,
    change_id: changeId,
  }
}

/** The ``owner/repo`` a run belongs to, lowercased for comparison.
 *
 * Repo-wide runs carry it directly. Runs started from picked or pasted PR links
 * do not, so it is recovered from the first change URL — that is what lets the
 * middle column show only the selected repo's reviews. Returns null when the
 * link is not a shape we recognise (a run we cannot attribute is only ever
 * hidden from a repo-scoped list, never from the rail's full list). */
export function repoOfRun(run: { repo?: string; changes: string[] }): string | null {
  if (run.repo && run.repo.includes('/')) return run.repo.toLowerCase()
  const first = run.changes?.[0]
  if (!first) return null
  const m = /(?:github\.com|gitlab\.com)\/([^/\s]+)\/([^/\s]+)/i.exec(first)
  return m ? `${m[1]}/${m[2]}`.toLowerCase() : null
}

/** The lossless identity of a change, for matching a PR against a run.
 *
 * `change_id` is NOT safe to match on. The backend derives it with
 * `change_id_for`, which is also an on-disk filename and so is lossily
 * sanitized (`[^A-Za-z0-9._-]+` collapses to `_`). Two different repositories
 * therefore share one id: `acme/service-api#5` and `acme/service_api#5` both
 * become the same stem. The backend already hit this — it is why the durable
 * reviewed-index uses `reviewed_key_for` instead — but the UI kept matching on
 * the lossy id, so a collision selected the wrong run, scoped the PR pane to a
 * stranger's review, and posted comments to the other pull request.
 *
 * The PR URL is the identity that does not collapse, and `run.changes` holds
 * exactly those URLs. Normalized only for case and a trailing slash, since the
 * same PR can arrive from the picker and from a pasted link.
 */
export function changeKey(url: string): string {
  return String(url ?? '').trim().replace(/\/+$/, '').toLowerCase()
}

/** Whether `run` covers the change at `url`. */
export function runCoversChange(
  run: { changes?: string[] }, url: string,
): boolean {
  const want = changeKey(url)
  if (!want) return false
  return (run.changes ?? []).some((c) => changeKey(c) === want)
}

/** The status to SHOW for a run, which is not always the one stored on it.
 *
 * ``run_review`` reports ok for any run with at least one change, so a run whose
 * every change failed was recorded as "done". The backend now writes "error" for
 * that case, but runs recorded BEFORE it did keep their misleading status on
 * disk, and a green "Done" beside "0 / 1 reviewed · 1 failed" is exactly the
 * contradiction this resolves. Derived from the per-change phases, which are the
 * ground truth about what actually happened.
 *
 * Only ever demotes a claimed success — a run the backend already calls error,
 * cancelled or running is passed through untouched. */
export function effectiveRunStatus(run: {
  status: RunStatus
  changes: string[]
  change_ids?: string[]
  progress?: Record<string, { phase?: string }>
}): RunStatus {
  if (run.status !== 'done') return run.status
  let done = 0
  let failed = 0
  run.changes.forEach((change, i) => {
    const cid = run.change_ids?.[i] ?? change
    const phase = run.progress?.[cid]?.phase
    if (phase === 'done') done += 1
    else if (phase === 'failed') failed += 1
  })
  return done === 0 && failed > 0 ? 'error' : 'done'
}

/** Median wall-clock duration of past SUCCESSFUL runs of the same size, in ms,
 * or null when there is not enough history to say.
 *
 * A single-PR review is one opaque worker turn, so there is no percentage to
 * show — but "usually about 7 min" turns an indefinite wait into a bounded one.
 * Median rather than mean because one timed-out run would otherwise dominate,
 * and only runs of the same change count, since duration scales with them. */
export function typicalRunMs(runs: {
  status: RunStatus
  changes: string[]
  started_at: string
  finished_at?: string
}[], changeCount: number): number | null {
  const spans: number[] = []
  for (const r of runs) {
    if (r.status !== 'done' || !r.finished_at) continue
    if (r.changes.length !== changeCount) continue
    const ms = Date.parse(r.finished_at) - Date.parse(r.started_at)
    if (Number.isFinite(ms) && ms > 0) spans.push(ms)
  }
  // Two samples is the floor: one run is an anecdote, not an expectation.
  if (spans.length < 2) return null
  spans.sort((a, b) => a - b)
  const mid = Math.floor(spans.length / 2)
  return spans.length % 2 === 0 ? (spans[mid - 1] + spans[mid]) / 2 : spans[mid]
}

/** Why a run failed, in words a reader can act on.
 *
 * The driver records causes in its own vocabulary ("Runtime process died during
 * prompt", "no_review_recorded"). Those are accurate but say nothing about what
 * happened or what to do, so the known ones are translated and anything
 * unrecognised is passed through verbatim rather than flattened into a generic
 * message. Returns null when the run did not fail. */
export function failureReason(run: {
  status: RunStatus
  error?: string
  changes: string[]
  change_ids?: string[]
  progress?: Record<string, { phase?: string; error?: string }>
}, changeId?: string): { text: string; raw: string } | null {
  if (effectiveRunStatus(run) !== 'error' && run.status !== 'interrupted') return null
  // Prefer the per-change cause when a specific change is in view: on a
  // multi-PR run the run-level error may belong to a different one.
  let raw = ''
  if (changeId) raw = (run.progress?.[changeId]?.error || '').trim()
  if (!raw) raw = (run.error || '').trim()
  if (!raw) {
    for (const change of run.changes) {
      const cid = run.change_ids?.[run.changes.indexOf(change)] ?? change
      const e = (run.progress?.[cid]?.error || '').trim()
      if (e) { raw = e; break }
    }
  }
  if (!raw) return { text: i18nT('apps.codeReviewSage.lib.format.cause_unknown'), raw: '' }

  const lower = raw.toLowerCase()
  // Matched with regex literals rather than string literals: these are
  // fragments of BACKEND error text, compared and never rendered, so they must
  // not be translated (and the i18n lint rightly flags bare English strings).
  if (/runtime process died|runtime is dead/.test(lower)) {
    return {
      // The common cause by far, and not the review's fault — restarting the
      // gateway takes the reviewer down with it.
      text: i18nT('apps.codeReviewSage.lib.format.cause_runtime_died'),
      raw,
    }
  }
  if (/no result record|no_review_recorded/.test(lower)) {
    return { text: i18nT('apps.codeReviewSage.lib.format.cause_no_record'), raw }
  }
  if (/timed out|timeout/.test(lower)) {
    return { text: i18nT('apps.codeReviewSage.lib.format.cause_timeout'), raw }
  }
  if (/review_failed/.test(lower)) {
    return { text: i18nT('apps.codeReviewSage.lib.format.cause_turn_failed'), raw }
  }
  return { text: raw, raw }
}

/** Build the `PullRequestLink` the shared `PullRequestPanel` expects from a Sage
 *  change URL, or `null` when the URL is not a GitHub pull request.
 *
 *  Parsing rather than trusting: the URL reaches the panel, which puts it in an
 *  `href` and in provider calls, so a shape that does not match a pull request is
 *  refused here instead of being rendered.
 */
export function sageSourceLink(url: string): PullRequestLink | null {
  const m = url.match(/^https:\/\/github\.com\/([^/\s]+)\/([^/\s]+)\/pull\/(\d+)$/)
  if (!m) return null
  return {
    url, provider: 'github', number: Number(m[3]), repo: `${m[1]}/${m[2]}`, kind: 'change',
  }
}
