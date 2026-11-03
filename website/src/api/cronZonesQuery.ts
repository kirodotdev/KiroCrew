import { api } from './client'

/** Where a job with no zone of its own runs, as GET /api/crons reports it. */
export interface CronZones {
  /** The IANA zone 'local' stands for now (the computer Kiro Crew runs on), or
   *  null while the server has not detected it. */
  hostTz: string | null
  /** What a job with no zone of its own follows: the configured zone, or 'local'. */
  defaultTz: string
}

/**
 * The zones the job form names. A separate key from ['cron-jobs'], whose queryFn
 * keeps only the job list: two observers of one key must share one queryFn. Under
 * ['crons'] so every cron refresh frame invalidates it (serverState's CRON_TARGETS
 * match that prefix), and re-read each minute while a form is open, because a
 * config change or a computer that travels pushes no cron frame.
 */
export const cronZonesQuery = {
  queryKey: ['crons', 'zones'] as const,
  queryFn: (): Promise<CronZones> =>
    api.crons().then((r) => ({ hostTz: r.host_tz ?? null, defaultTz: r.default_tz || 'local' })),
  staleTime: 60_000,
  refetchInterval: 60_000,
}
