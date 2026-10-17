/**
 * A failed scheduled-job run, packaged for the "Ask the agent" hand-off.
 *
 * Model-facing text only, per the `*.prompt.ts` convention in
 * `eslint.i18n.config.js`: these fields become the fenced fact block of the
 * prompt `buildErrorPrompt` assembles, so they are English by design and carry
 * no UI copy. The trace is attacker-reachable (a job's output can quote any
 * remote server), which is exactly what that prompt's untrusted-data fence and
 * `redactSecrets` scrub are for; this module adds no second channel around them.
 */

import { redactSecrets, type ErrorReport } from './errorReport'
import { toDate } from '../i18n/format'

/**
 * How much of a run's trace rides along. The TAIL is kept, not the head: a run
 * that fails reports why at the end, and the job id plus run id let the agent
 * read the rest of the history itself.
 */
export const MAX_TRACE_TAIL = 4000
const HEAD_ELIDED = '[earlier output truncated]\n'

/** The statuses a run row offers the hand-off for. `cancelled` is a user's own act, not a failure. */
export function isFailedRunStatus(status: string): status is 'failure' | 'timeout' {
  return status === 'failure' || status === 'timeout'
}

/** Scrub THEN cut, so a cut can never drop the anchor a redaction pattern needs (see `redactThenCap`). */
function traceTail(trace: string): string {
  const redacted = redactSecrets(trace.trim())
  return redacted.length > MAX_TRACE_TAIL ? HEAD_ELIDED + redacted.slice(-MAX_TRACE_TAIL) : redacted
}

export function buildCronRunReport(args: {
  jobId: string
  jobName?: string
  runId: string
  status: 'failure' | 'timeout'
  startedAt: number
  trigger: string
  summary?: string
  trace?: string
}): ErrorReport {
  const name = args.jobName?.trim() || args.jobId
  const outcome = args.status === 'timeout' ? 'timed out' : 'failed'
  const summary = args.summary?.trim()
  const lines = [
    `Job id: ${args.jobId}`,
    `Run id: ${args.runId}`,
    `Trigger: ${args.trigger}`,
  ]
  const started = toDate(args.startedAt)
  if (started) lines.push(`Started: ${started.toISOString()}`)
  if (summary) lines.push(`Summary: ${summary}`)
  const tail = args.trace ? traceTail(args.trace) : ''
  if (tail) lines.push('', 'Run output (tail):', tail)
  return {
    id: `cron-run-${args.runId}`,
    at: Date.now(),
    source: 'system',
    code: `cron_run_${args.status}`,
    route: '/schedule',
    message: `Scheduled job "${name}" ${outcome}.`,
    detail: lines.join('\n'),
  }
}
