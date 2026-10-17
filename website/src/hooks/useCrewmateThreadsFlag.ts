/**
 * `dashboard.crewmate_threads` -- whether reply threads on crewmate chat messages
 * are on for this gateway. A server-side config flag (`config/sections.py`),
 * off by default, read from the shared `['kirocrewConfig']` cache so the Members
 * page and the Settings toggle agree without a second request.
 *
 * Three readings, kept apart so a failed read cannot pass for "off":
 * - `on`: the config was read and says `true`. `false` while the flag is off,
 *   before the config has loaded, or when a first read failed with nothing
 *   cached -- the routes behind the feature answer 404 while it is off, so a
 *   control drawn on a stale `true` would only reach a refusal; defaulting
 *   closed is the honest reading of "not known to be on". A refetch that fails
 *   keeps the last known value (React Query keeps `data` through an error), so
 *   an open thread does not vanish under a blip.
 * - `failed`: the read itself failed. The page says so through the standard
 *   `ErrorNotice` path and offers `retry`; it never renders the failure as a
 *   silently missing feature.
 */
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

export const CREWMATE_THREADS_CONFIG_KEY = 'dashboard.crewmate_threads'

type KirocrewConfigThreads = { dashboard?: { crewmate_threads?: boolean } }

export type CrewmateThreadsFlag = {
  /** The flag is known to be on (last successful read said `true`). */
  on: boolean
  /** The config read failed; `on` is then the last known value, or `false` with none. */
  failed: boolean
  /** A read is in flight (the Retry control is held while it runs). */
  retrying: boolean
  /** Read the config again. */
  retry: () => void
}

export function useCrewmateThreadsFlag(): CrewmateThreadsFlag {
  const q = useQuery<KirocrewConfigThreads, Error, boolean>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    select: (c) => c?.dashboard?.crewmate_threads === true,
  })
  return {
    on: q.data === true,
    failed: q.isError,
    retrying: q.isFetching,
    retry: () => { void q.refetch() },
  }
}
