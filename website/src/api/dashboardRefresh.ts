/** How an open dashboard re-reads its page after the gateway says it moved.
 *
 *  A crewmate that is working moves many times a second, and a read costs the
 *  gateway about a core while it runs. `invalidateQueries` with its default
 *  `cancelRefetch` cancels the query's fetch and begins another, but the request it
 *  walks away from is a plain `fetch` with no signal, so it runs on and the gateway
 *  builds an answer nobody will look at. Frames that arrive faster than one read
 *  takes would stack reads up without bound, and every other request the page makes
 *  (its document mint, the next frame's read) would wait behind them.
 *
 *  So a read in flight is never cancelled or doubled, the rule `contextTraceRefresh`
 *  and `sessionProjection` follow for their queries. A frame that lands during one
 *  owes exactly one more read once it settles, however many frames land, so the
 *  newer read lands last and the page ends on the newest state. That owed read also
 *  covers a fetch the frame did not start (the fallback poll, a mount read): it began
 *  before the write the frame announces, and the read after it does not.
 *
 *  With no dashboard open under the key, a frame is a plain `invalidateQueries`: it
 *  marks the cached read stale and asks the gateway for nothing.
 *
 *  Nothing here decides what a read returns: ownership, redaction and the page are
 *  the route's. This only decides when the route is asked.
 */
import type { Query, QueryClient, QueryKey } from '@tanstack/react-query'

/** Queries with a follow-up read already owed, whatever number of frames asked for it. */
const owed = new WeakSet<Query>()

/** Re-read every open query under *queryKey*, one read at a time per query. */
export function refreshDashboardReads(queryClient: QueryClient, queryKey: QueryKey): void {
  const cached = queryClient.getQueryCache().findAll({ queryKey })
  if (!cached.some((query) => query.isActive())) {
    void queryClient.invalidateQueries({ queryKey })
    return
  }
  for (const query of cached) readNow(queryClient, query)
}

/** One re-read of *query*: now when none is out, otherwise once after the one that is. */
function readNow(queryClient: QueryClient, query: Query): void {
  if (query.state.fetchStatus === 'fetching' && query.promise) {
    if (owed.has(query)) return
    owed.add(query)
    void query.promise
      .catch(() => undefined)
      .finally(() => {
        owed.delete(query)
        void queryClient.invalidateQueries({ queryKey: query.queryKey, exact: true }, { cancelRefetch: false })
      })
    return
  }
  void queryClient.invalidateQueries({ queryKey: query.queryKey, exact: true }, { cancelRefetch: false })
}
