import { api } from './client'
import type { CronJob } from '../types'

/**
 * The ONE definition of the shared ['cron-jobs'] query — the flat job list.
 *
 * Read by the composer's watch popover and the Crew Members drawer's
 * wake-sources block; the Schedule page's ExecutionsView still spells the
 * same key + queryFn inline (moving it there costs a covered statement in a
 * coverage-baselined file — migrate it when that file gains tests). Every
 * consumer spreads this object rather than restating the key: react-query
 * hands whichever observer mounts first the shape its queryFn produced, so
 * two inline spellings with different queryFns would let one decide the
 * other's data. useWebSocket invalidates this key on every server `refresh`
 * frame, which is where its freshness comes from (the global staleTime is
 * Infinity).
 */
export const cronJobsQuery = {
  queryKey: ['cron-jobs'] as const,
  queryFn: (): Promise<CronJob[]> => api.crons().then((r) => (r.jobs || []) as CronJob[]),
}
