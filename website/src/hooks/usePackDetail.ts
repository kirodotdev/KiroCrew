/**
 * One appearance pack's detail, read through React Query.
 *
 * Query key: `['appearance-pack', id]`.
 *
 * The detail route inlines every file in the pack, so the budget is one read per
 * pack per session however many avatars wear it — React Query's in-flight
 * sharing and `staleTime: Infinity` give exactly that. Retry is the shared
 * client's policy (`api/queryClient.ts`: one retry with backoff, a longer ladder
 * on 429 throttles, none on a deadline we set) rather than a per-hook number, so
 * a pack read behaves like every other dashboard read when the gateway is
 * throttled or down. A query left in error state has no data, so React Query
 * refetches it on the next mount, window focus or reconnect, which is how an
 * avatar recovers once the gateway is back.
 *
 * Invalidating the key notifies every mounted subscriber, so a pack re-imported
 * under the same id redraws on every roster row that wears it, not only on the
 * Library tab that did the importing.
 */
import { useCallback } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import { packDetailFrom, type PackDetail } from '../lib/appearancePacks/detail'

const packDetailQueryKey = (id: string) => ['appearance-pack', id] as const

export function usePackDetail(id: string) {
  return useQuery<PackDetail>({
    queryKey: packDetailQueryKey(id),
    queryFn: async () => packDetailFrom(await api.appearances.detail(id)),
    staleTime: Infinity,
  })
}

/** Forget one pack. The Library tab calls it after an import or a delete — both
 *  change what that id resolves to — and every mounted avatar re-reads. */
export function useInvalidatePackDetail(): (id: string) => void {
  const qc = useQueryClient()
  return useCallback(
    (id: string) => {
      void qc.invalidateQueries({ queryKey: packDetailQueryKey(id) })
    },
    [qc],
  )
}
