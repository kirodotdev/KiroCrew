import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { readSessionSortKey, type SessionSortConfigBody, type SortKey } from '../pages/chat/sessionOrder'

export interface SessionSortKeyRead {
  /**
   * The order to draw sessions in: the person's stored `dashboard.session_sort`
   * once a config body is on hand (fresh or cached), the default before one
   * arrives, after a read failed with none to fall back on, or from an older
   * gateway without the field.
   */
  readonly sortKey: SortKey
  /** A config body has been read, so `sortKey` is the stored one, not the fallback. */
  readonly known: boolean
}

/**
 * The person's sidebar session sort (`dashboard.session_sort`), read through the
 * shared `['kirocrewConfig']` query rather than a fetch of its own: the sidebar
 * already holds that query, and the sort menu settles its write back into the
 * same cache entry, so every reader switches together when a save lands.
 */
export function useSessionSortKey(): SessionSortKeyRead {
  const q = useQuery<SessionSortConfigBody>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  return {
    sortKey: readSessionSortKey(q.data?.dashboard?.session_sort),
    known: q.data !== undefined,
  }
}
