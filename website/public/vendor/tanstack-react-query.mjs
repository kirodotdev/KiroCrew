// Vendor stub: re-exports the host's React Query instance, including its context.
// Keep runtime exports in sync with the pinned dependency; appSharedModuleBoundary
// verifies names and identity so apps never bundle a second QueryClientContext.
const m = window.__kirocrew_modules?.['@tanstack/react-query']
if (!m) throw new Error('[vendor/tanstack-react-query] Host modules not initialized.')
export const {
  CancelledError, HydrationBoundary, InfiniteQueryObserver, IsRestoringProvider,
  Mutation, MutationCache, MutationObserver, QueriesObserver, Query, QueryCache,
  QueryClient, QueryClientContext, QueryClientProvider, QueryErrorResetBoundary,
  QueryObserver, dataTagErrorSymbol, dataTagSymbol, defaultScheduler,
  defaultShouldDehydrateMutation, defaultShouldDehydrateQuery, dehydrate,
  environmentManager, experimental_streamedQuery, focusManager, hashKey, hydrate,
  infiniteQueryOptions, isCancelledError, isServer, keepPreviousData, matchMutation,
  matchQuery, mutationOptions, noop, notifyManager, onlineManager, partialMatchKey,
  queryOptions, replaceEqualDeep, shouldThrowError, skipToken, timeoutManager,
  unsetMarker, useInfiniteQuery, useIsFetching, useIsMutating, useIsRestoring,
  useMutation, useMutationState, usePrefetchInfiniteQuery, usePrefetchQuery,
  useQueries, useQuery, useQueryClient, useQueryErrorResetBoundary,
  useSuspenseInfiniteQuery, useSuspenseQueries, useSuspenseQuery,
} = m
