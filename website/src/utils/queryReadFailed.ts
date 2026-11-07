/** A read failed and has not succeeded since. A retry without cached data
 *  stays visibly failed while pending, rather than blinking out the notice:
 *  React Query clears `error` and returns to `pending` when a query with no
 *  data refetches, so a surface keyed on `error` alone unmounts its own Retry
 *  button, and the focus on it, for the whole retry. */
export function queryReadFailed(q: { isError: boolean; isPending: boolean; errorUpdateCount: number }): boolean {
  return q.isError || (q.isPending && q.errorUpdateCount > 0)
}
