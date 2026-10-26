import backendPhrases from '../lib/backendPhrases.json'
import type { CronJob } from '../types'

/** The opening words of the reason an agent cron run carries when the security
 *  gate refused SOME of its tool calls while others ran
 *  (`_GateTally.refusal_summary()`, backend): "1 of 54 tool call(s) blocked by
 *  the security gate: ...". A run whose EVERY call was refused opens with
 *  "all N" instead and does not match, because that run did nothing.
 *
 *  It lives in `lib/backendPhrases.json`, not as a literal here, because it is
 *  backend prose this UI MATCHES rather than copy it renders.
 *  `test_the_schedule_page_tells_a_partial_block_from_a_total_one` pins it
 *  against the reason the backend writes. */
const PARTIAL_GATE_BLOCK = new RegExp(backendPhrases.cronPartialGateBlockPattern)

/** Whether a job's last run did its work but lost one or more tool calls to
 *  the security gate.
 *
 *  The gateway records that run as `last_status: "error"` on purpose: the lost
 *  call means the run did not fully succeed, so it must not read as `ok` or
 *  reset a failure streak. But it also spent nothing from the failure budget,
 *  and a plain "Error" would make a run that did all but one call look like
 *  one that died. The Schedule page reads this to show it as its own state
 *  instead. Script and command jobs never pass through the tool gate, so their
 *  errors are never read this way. */
export function isPartlyBlocked(job: Pick<CronJob, 'last_status' | 'last_error' | 'script' | 'command'>): boolean {
  if (job.last_status !== 'error' || job.script || job.command) return false
  return typeof job.last_error === 'string' && PARTIAL_GATE_BLOCK.test(job.last_error)
}
