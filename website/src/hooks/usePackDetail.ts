/**
 * One appearance pack's detail, read through React Query.
 *
 * Query key: `['appearance-pack', id]` for the meta read, and
 * `['appearance-pack', id, 'content']` for the whole pack.
 *
 * The budget is one read of each kind per pack per session however many avatars
 * wear it — React Query's in-flight sharing and `staleTime: Infinity` give
 * exactly that.
 * Retry is the shared client's policy (`api/queryClient.ts`: one retry with
 * backoff, a longer ladder on 429 throttles, none on a deadline we set) rather
 * than a per-hook number, so a pack read behaves like every other dashboard read
 * when the gateway is throttled or down. A query left in error state has no
 * data, so React Query refetches it on the next mount, window focus or
 * reconnect, which is how an avatar recovers once the gateway is back.
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

/**
 * `id` is NULLABLE so a caller that does not yet know whether the crew wears a
 * pack can call this unconditionally — a hook cannot be conditional, and the cue
 * side needs the detail one level above the renderer that already reads it.
 * Disabled means no request at all, and the key is stable either way, so a crew
 * that puts a pack on starts sharing the query the roster already warmed.
 *
 * The read is the META answer (`?meta=1`): each slot's format, the sprite
 * geometry and the cue presence, and no file's bytes. That is all the svg and
 * sprite tiers and the cue side need. `content: true` reads the whole pack, for
 * the one tier that parses bytes in the page — a Lottie slot. Its key sits under
 * the pack's, so invalidating the pack forgets both reads.
 */
export function usePackDetail(
  id: string | null | undefined,
  { content = false }: { content?: boolean } = {},
) {
  const key = packDetailQueryKey(id ?? '')
  const meta = !content
  return useQuery<PackDetail>({
    queryKey: meta ? key : [...key, 'content'],
    queryFn: async () => packDetailFrom(await api.appearances.detail(id as string, { meta }), { meta }),
    enabled: !!id,
    staleTime: Infinity,
  })
}

/** Forget one pack — both its reads. The Library tab calls it after an import or
 *  a delete — both change what that id resolves to — and every mounted avatar
 *  re-reads. */
export function useInvalidatePackDetail(): (id: string) => void {
  const qc = useQueryClient()
  return useCallback(
    (id: string) => {
      void qc.invalidateQueries({ queryKey: packDetailQueryKey(id) })
    },
    [qc],
  )
}
