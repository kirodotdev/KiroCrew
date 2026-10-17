/**
 * `useAppQuery` — a query whose key is namespaced to the app by the HOST.
 *
 * An app page has two things it cannot do for itself. It cannot prove which keys
 * in the shared query cache are its own, and it cannot decide how long its data
 * survives leaving the page. This module supplies both, on top of the identity
 * `./identity` publishes.
 *
 *   useAppQuery(['drive', account], { queryFn })  →  ['aws-control', 'drive', account]
 *
 * The prefix comes from React context, so an app cannot forge it, and code that
 * genuinely wants the HOST's cache keeps using plain `useQuery`. That difference
 * is greppable, which is the point rather than a side effect: several key
 * prefixes are shared between an app and the host deliberately — `artifact`,
 * `awsConsent`, `apps`, `pull-request-source`, `workflow-definitions` — and the
 * thing worth expressing is not "every key belongs to an app" but "which keys
 * are mine and which are shared".
 *
 * Deliberately NOT on the `./index` barrel, following the same reasoning
 * `useComposerDraft` records there: `chatProtocolBoundary.test.ts` holds that
 * barrel in exact agreement with `public/vendor/kirocrew-app-sdk.mjs`, so a name
 * on it is PUBLISHED to third-party apps — and a hook that hands out a
 * host-namespaced cache client is precisely what must not be published. In-tree
 * callers import this module by path.
 *
 * There is no `useAppInfiniteQuery`. An infinite query — or a `setQueryData`, or
 * an `invalidateQueries` — uses `useAppQueryKey()` and stays on the plain
 * react-query hook, so one key resolver serves every cache API instead of this
 * module growing a wrapper per hook. Adding a wrapper later is additive.
 */
import { useCallback } from 'react'
import {
  useQuery,
  type DefaultError,
  type QueryKey,
  type UseQueryOptions,
  type UseQueryResult,
} from '@tanstack/react-query'
import { queryClient as sharedQueryClient } from '../api/queryClient'
import { useTrustedAppId } from './identity'
import {
  applyCacheRetention,
  resolveAppQueryKey,
  resolveCacheRetention,
  type QueryDefaultsSink,
} from '../apps/appCacheRetention'

/**
 * Clients already carrying a given app's retention default.
 *
 * Retention is one registration per client and app, for the life of the client:
 * react-query warns when several query defaults match one key, and re-registering
 * on every render would write the same value thousands of times. Weakly keyed so
 * a test client is collectable with its entry.
 */
const retentionRegistered = new WeakMap<QueryDefaultsSink, Set<string>>()

/**
 * Register this app's cache retention, synchronously.
 *
 * Module-private: the only caller is `AppCacheRetention` below, and exporting a
 * hook nothing outside this file mounts would invite a second retention seam
 * that the per-client memo cannot see. The component is the public shape.
 *
 * Returns nothing on purpose: its caller renders no UI from it, and the plan is
 * already available purely from `resolveCacheRetention`, so handing it back would
 * be a second way to obtain the same value that nothing asked for.
 *
 * **The synchronous part is the feature.** This runs in a render body, never in
 * an effect, because a query default only applies to what is read after it is
 * set — the same ordering issue-radar solves by putting its own
 * `setQueryDefaults` call at module scope. An effect version passes a cold-load
 * test and fails a user: on a cold load the page module is still being fetched,
 * so Suspense holds the child back until after the parent's effects have run,
 * and the ordering bug is invisible. On a REPEAT visit the module is already
 * loaded, React renders parent and child in one pass, and an effect is a render
 * too late — which is exactly the visit this whole design exists for.
 *
 * Defaults to the shared client rather than reading `useQueryClient()`, matching
 * the precedent this replaces (issue-radar imports the same singleton for the
 * same call). `main.tsx` passes this instance to `QueryClientProvider`, so it is
 * the client every app query actually lands on; taking it as a parameter keeps
 * the rule verifiable against a recording fake, and means a host page that
 * mounts outside a provider cannot be made to throw by adding retention.
 */
function useAppCacheRetention(client: QueryDefaultsSink = sharedQueryClient): void {
  const appId = useTrustedAppId()
  const plan = resolveCacheRetention(appId)
  if (!plan) return
  let registered = retentionRegistered.get(client)
  if (!registered) {
    registered = new Set<string>()
    retentionRegistered.set(client, registered)
  }
  const [prefix] = plan.keyPrefix
  if (!registered.has(prefix)) {
    registered.add(prefix)
    applyCacheRetention(client, plan)
  }
}

/**
 * Declares "the app owning this subtree keeps its cached data".
 *
 * Renders nothing. Mount it as the first child of the identity provider, where
 * it reads the appId from context and registers retention before the page below
 * it renders — React reconciles siblings in order, so a sibling ahead of the
 * page's `Suspense` boundary is ahead of the page's first query.
 *
 * It reads the gate through `useTrustedAppId()`, so a host page and an external
 * app both register nothing, without this component knowing why.
 */
export function AppCacheRetention({ client }: { client?: QueryDefaultsSink } = {}): null {
  useAppCacheRetention(client)
  return null
}

/**
 * The app-namespaced form of a key, for the cache APIs that are not `useQuery`.
 *
 * `invalidateQueries`, `setQueryData`, `removeQueries` and `useInfiniteQuery` all
 * take a key rather than being a query hook, and every one of them must agree
 * with `useAppQuery` byte for byte or a mutation stops invalidating the list it
 * just changed. Sharing the resolver is what makes that agreement structural
 * instead of a convention two call sites have to remember.
 */
export function useAppQueryKey(): (key: QueryKey) => QueryKey {
  const appId = useTrustedAppId()
  return useCallback((key: QueryKey) => resolveAppQueryKey(appId, key), [appId])
}

/**
 * `useQuery`, with the key namespaced to the app that owns this page.
 *
 * The key is a positional argument and `queryKey` is removed from the options
 * type, so a caller cannot pass a key the host did not prefix — the namespace is
 * not a convention this hook asks callers to follow.
 *
 * On a host page or in an external app `useTrustedAppId()` is `null` and the key
 * is used unchanged. That degrades to exactly `useQuery`, which is the right
 * failure: an un-namespaced key still fetches and still shares correctly with
 * any other reader of the same key, where inventing a fallback prefix would put
 * the data somewhere no other reader looks.
 */
export function useAppQuery<TQueryFnData = unknown, TError = DefaultError, TData = TQueryFnData>(
  key: QueryKey,
  options: Omit<UseQueryOptions<TQueryFnData, TError, TData, QueryKey>, 'queryKey'>,
): UseQueryResult<TData, TError> {
  const appId = useTrustedAppId()
  return useQuery<TQueryFnData, TError, TData, QueryKey>({
    ...options,
    queryKey: resolveAppQueryKey(appId, key),
  })
}
