/**
 * The last SETTLED outcome of a read was a failure, and no data ever arrived.
 *
 * Not `isError`: a retry resets a data-less errored query to `pending` with
 * its error cleared, so a notice keyed on `isError` would vanish for the whole
 * retry and come back.
 */
export function failedWithNoData(q: { data: unknown; errorUpdatedAt: number }): boolean {
  return q.data === undefined && q.errorUpdatedAt > 0
}
