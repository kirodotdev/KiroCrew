import type { CronJob } from '../types'

/** When a job's claimed run starts executing (epoch seconds), or null.
 *
 *  A recurring job that is not on a strict schedule starts a random time after
 *  its schedule. For that wait the gateway already holds the run, so
 *  `is_running` is true, and nothing has run yet: the Schedule page shows the
 *  job as waiting, with this time, instead of as running. An older gateway
 *  sends no `waiting_until`, and the job reads as running. */
export function runWaitingUntil(job: Pick<CronJob, 'is_running' | 'waiting_until'>): number | null {
  if (!job.is_running) return null
  return typeof job.waiting_until === 'number' && job.waiting_until > 0 ? job.waiting_until : null
}
