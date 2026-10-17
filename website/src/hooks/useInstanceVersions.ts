/**
 * The shared `GET /api/instances/versions` read: every connected crew's version
 * beside this machine's, with `behind` decided by the gateway.
 *
 * One query key for every reader (each Remote Crew row, the post-update offer),
 * so react-query serves them all from one request. The key sits under
 * `['instances']`, so the invalidation every connect, disconnect and edit
 * already issues re-reads the versions too.
 */
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

export const INSTANCE_VERSIONS_KEY = ['instances', 'versions'] as const

/** How long a versions read stays fresh: a remote's version only moves when it
 *  is updated and restarted, which the reconnect invalidation already covers. */
const VERSIONS_STALE_MS = 60_000

export function useInstanceVersions(
  enabled: boolean,
  refetchInterval?: (data: Awaited<ReturnType<typeof api.instancesVersions>> | undefined, reads: number) => number | false,
) {
  return useQuery({
    queryKey: INSTANCE_VERSIONS_KEY,
    queryFn: () => api.instancesVersions(),
    enabled,
    staleTime: VERSIONS_STALE_MS,
    retry: false,
    refetchInterval: refetchInterval
      ? q => refetchInterval(q.state.data, q.state.dataUpdateCount + q.state.errorUpdateCount)
      : false,
  })
}
