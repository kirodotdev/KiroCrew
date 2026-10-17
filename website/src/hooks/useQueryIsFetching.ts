/**
 * Whether ONE cached query is fetching right now, keyed exactly.
 *
 * `useIsFetching({ queryKey, exact: true })` answers the same question, but it
 * subscribes to the whole QueryCache and, on every cache event, re-runs
 * `queryClient.isFetching`, which hashes the filter key once per cached query.
 * Every `useQuery` whose options object is not shallow-equal to its last render
 * emits such an event, so a composer that renders on each keystroke paid for
 * thousands of key hashes per keystroke through that one subscription.
 *
 * This hook hashes the key once per render and, per cache event, compares one
 * string. It only answers for queries that use the default key hash (a query
 * with its own `queryKeyHashFn` stores a different `queryHash`).
 */
import { useCallback, useSyncExternalStore } from 'react'
import { hashKey, notifyManager, useQueryClient, type QueryKey } from '@tanstack/react-query'

export function useQueryIsFetching(queryKey: QueryKey): boolean {
  const queryClient = useQueryClient()
  const hash = hashKey(queryKey)
  const subscribe = useCallback(
    (onChange: () => void) => {
      // The cache can emit 'added' synchronously while another component
      // renders (a useQuery building an evicted key), so defer the store
      // notification the way useIsFetching does, via notifyManager.batchCalls.
      const notify = notifyManager.batchCalls(onChange)
      return queryClient.getQueryCache().subscribe(event => {
        if (event.query.queryHash === hash) notify()
      })
    },
    [queryClient, hash],
  )
  const getSnapshot = useCallback(
    () => queryClient.getQueryCache().get(hash)?.state.fetchStatus === 'fetching',
    [queryClient, hash],
  )
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}
