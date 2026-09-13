import { useCallback, useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { searchErrorCause, type SearchErrorCause } from '../lib/searchErrorCause'

export type BrowseListError = false | SearchErrorCause

type BrowseDirsResult = Awaited<ReturnType<typeof api.browseDirs>>

/**
 * The directory drill both pickers need, as a React Query read (`AGENTS.md`: data fetching is
 * React Query, never a hand-rolled fetch) keyed on the path being browsed.
 *
 * `gcTime: 0` is what keeps the query form honest here: with no retained entry, reopening the
 * picker re-reads the filesystem instead of replaying rows that may no longer exist, so none of
 * the delivery-gating an earlier revision of this change needed is required. Focus and reconnect
 * refetching are off because the shared client leaves `refetchOnWindowFocus` on, and a background
 * re-read would re-fire the caller's success path over a path the user is still typing.
 *
 * `preserveInput` travels with the target rather than in a ref, so the flag the caller's success
 * path reads always belongs to the drill whose rows just arrived.
 */
export function useBrowseDirs(
  open: boolean,
  onData: (d: BrowseDirsResult, preserveInput: boolean) => void,
) {
  const [target, setTarget] = useState<{ path?: string; preserveInput: boolean } | null>(null)
  const onDataRef = useRef(onData)
  onDataRef.current = onData
  const queryClient = useQueryClient()

  const { data, dataUpdatedAt, error, isError, isFetching, refetch } = useQuery({
    queryKey: ['browse-dirs', target?.path ?? ''],
    queryFn: ({ signal }) => api.browseDirs(target?.path, signal),
    enabled: open && target !== null,
    staleTime: 0,
    gcTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: false,
  })

  // Closing abandons the drill rather than leaving it to land unread: cancelling aborts the
  // signal react-query passed to `queryFn`, so the request is dropped.
  useEffect(() => {
    if (open) return
    void queryClient.cancelQueries({ queryKey: ['browse-dirs'] })
  }, [open, queryClient])

  const browse = useCallback((path?: string, preserveInput = false) => {
    // A different path is a different key, which fetches it; the SAME path needs an explicit
    // refetch. The outcome reaches the caller through onData / listError, not a promise here.
    let samePath = false
    setTarget(t => {
      samePath = t !== null && t.path === path
      return { path, preserveInput }
    })
    if (samePath) void refetch()
  }, [refetch])

  const retry = useCallback(() => {
    // The failure left the user free to edit the path, so the retained target stops claiming
    // the input: a successful retry must not overwrite what they typed meanwhile.
    setTarget(t => (t === null ? t : { ...t, preserveInput: true }))
    // Resolves to whether the listing recovered, which is what decides where focus goes:
    // a refetch REJECTS only under `throwOnError`, so the result is the only signal.
    return refetch().then(r => !r.isError, () => false)
  }, [refetch])

  const deliveredRef = useRef(0)
  useEffect(() => {
    if (target === null || isFetching || data === undefined) return
    if (deliveredRef.current === dataUpdatedAt) return
    deliveredRef.current = dataUpdatedAt
    onDataRef.current(data, target.preserveInput)
  }, [data, dataUpdatedAt, isFetching, target])

  // Classified by the SHARED map, not a local timeout/failed split: a refusal re-asked returns
  // the same answer, so the pickers must be able to withhold Retry exactly as the @-menu does.
  const listError: BrowseListError = isError ? searchErrorCause(error) : false

  return { listError, browse, retry }
}
