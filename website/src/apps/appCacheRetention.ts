/**
 * An app's query-key namespace: what its keys are, and how long its data stays.
 *
 * Two decisions live here, both as pure functions over a trusted appId, for the
 * same reason `overlaySlots.ts` is pure: a rule that decides where a user's
 * cached data lands must be testable without mounting a tree.
 *
 *  - `resolveAppQueryKey` — the key an app's query actually gets.
 *  - `resolveCacheRetention` — how long an unmounted query under that namespace
 *    is kept before react-query garbage-collects it.
 *
 * The input is a TRUSTED appId, i.e. the return of `useTrustedAppId()`, whose
 * `null` means "this page gets no host-owned namespace" — a host page, or an
 * external app. Both functions degrade to plain behaviour on `null` rather than
 * inventing a prefix, so the builtin-only gate stays in one place and is not
 * re-decided here.
 */

/**
 * A react-query key, pinned locally rather than imported.
 *
 * Structurally identical to `@tanstack/react-query`'s `QueryKey`, and pinned for
 * the reason `overlaySlots.ts` pins its own subset of `GET /api/apps`: this
 * module is the rule, not the integration, and it must not need the library to
 * be testable.
 */
export type AppQueryKey = readonly unknown[]

/**
 * How long an UNMOUNTED app query's data is kept before react-query collects it.
 *
 * This is the dial that decides whether returning to an app feels instant. A page
 * is unmounted on the way out, so its data is retained only `gcTime` past the last
 * component reading it, and the app-wide default is react-query's 5 minutes --
 * shorter than an ordinary detour. Issue Radar measured the concrete version of
 * this before there was a platform to put it in: leave its Tagging dashboard for
 * six minutes, come back, and the queue has been evicted, so it shows a loading
 * line and refetches from scratch. Once per tab click.
 *
 * 30 minutes, and deliberately the number that app already ran in production
 * rather than a new one -- it was the one of nineteen that had solved this in
 * app-local code, and a platform mechanism whose point is that apps stop picking
 * their own number should not open by picking a second. Its own constant is
 * deleted in the same change, so this is the only one.
 *
 * The cost of a retained entry is memory, not requests: `staleTime` and the poll
 * intervals still decide WHEN a refetch happens, so a longer `gcTime` only decides
 * whether there is something to paint WHILE that refetch runs -- never "serve
 * something stale instead of fetching". Bounded rather than `Infinity`, so a
 * long-lived tab that has visited many apps does not retain all of their data for
 * the life of the tab.
 */
export const APP_CACHE_RETENTION_MS = 30 * 60_000

/** The one query-defaults registration an app's namespace needs. */
export interface CacheRetentionPlan {
  /**
   * The key prefix the retention is registered against — exactly `[appId]`.
   *
   * react-query matches query defaults by key PREFIX, so registering `[appId]`
   * covers every key under the app's namespace, including the ones an app still
   * writes by hand. That is what lets this fix an app with no change to the app.
   */
  readonly keyPrefix: readonly [string]
  readonly gcTime: number
}

/**
 * The retention to register for a page, or `null` to register nothing.
 *
 * `null` in, `null` out is the load-bearing case, not a guard clause: on a host
 * page or in an external app there is no app namespace, and registering a
 * default against some invented prefix would attach retention to keys that do
 * not belong to any app.
 */
export function resolveCacheRetention(trustedAppId: string | null): CacheRetentionPlan | null {
  if (!trustedAppId) return null
  return { keyPrefix: [trustedAppId], gcTime: APP_CACHE_RETENTION_MS }
}

/** appIds already reported as double-prefixing, so the console stays useful. */
const doublePrefixLogged = new Set<string>()

/**
 * The key a query under this app's namespace gets: `[appId, ...key]`.
 *
 * The prefix is exactly the appId — NOT `['app', appId, …]`. That is the only
 * shape under which two requirements hold at once: the host authors the prefix,
 * AND no existing app query key is renamed. AWS Control already writes
 * `['aws-control', 'drive', account]` by hand and its appId is `aws-control`, so
 * `resolveAppQueryKey('aws-control', ['drive', account])` is byte-identical to
 * what it writes today and every existing `invalidateQueries` keeps matching.
 * What changes is WHO authors the prefix, not what it is.
 *
 * A key that ALREADY starts with the appId is returned unchanged rather than
 * prefixed twice, and warns. This is the one mistake a conversion makes that
 * review does not catch: `['aws-control', 'aws-control', 'drive']` is a valid
 * key that fetches correctly and is never invalidated by the app's own
 * `['aws-control', 'drive', …]` mutations, so the symptom is stale data after a
 * write — far from the line that caused it. Collapsing keeps the key
 * byte-identical either way; the warning is what makes the slip fixable.
 */
export function resolveAppQueryKey(trustedAppId: string | null, key: AppQueryKey): AppQueryKey {
  if (!trustedAppId) return key
  if (key.length > 0 && key[0] === trustedAppId) {
    if (!doublePrefixLogged.has(trustedAppId)) {
      doublePrefixLogged.add(trustedAppId)
      // eslint-disable-next-line no-console -- a silently forked cache is invisible otherwise
      console.warn(
        `[app-sdk] useAppQuery key for "${trustedAppId}" already starts with the appId; ` +
          `the host adds that prefix. Drop it from the key — keeping it would fork this ` +
          `query off the namespace the app's own invalidations match.`,
      )
    }
    return key
  }
  return [trustedAppId, ...key]
}

/**
 * The narrow subset of `QueryClient` retention is written through.
 *
 * A structural type rather than the class, so the rule can be verified against a
 * recording fake — the property that matters is "one registration, against this
 * prefix, with this gcTime", and that is not worth a real client to observe.
 */
export interface QueryDefaultsSink {
  setQueryDefaults(queryKey: AppQueryKey, options: { gcTime: number }): void
}

/**
 * Register a plan against a client.
 *
 * Takes a NON-NULL plan and returns nothing. Both are deliberate: the sole
 * caller resolves the plan and returns early when there is none, so a runtime
 * null-check here would be unreachable and a boolean nobody reads would only
 * suggest a caller was meant to branch on it. Making the parameter non-nullable
 * moves that guarantee into the type, where it is checked at every future call
 * site instead of at one that already knows the answer.
 *
 * The caller is also responsible for calling this once per client and app,
 * because react-query keys query defaults by the hashed key -- a second call
 * with the same prefix silently replaces the first rather than adding to it.
 */
export function applyCacheRetention(sink: QueryDefaultsSink, plan: CacheRetentionPlan): void {
  sink.setQueryDefaults(plan.keyPrefix, { gcTime: plan.gcTime })
}
