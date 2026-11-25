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

/**
 * The same test for a read whose `null` is a reading with nothing in it (a
 * cold cache): no usable data is held, and the last SETTLED outcome is an
 * error. `failedWithNoData` is this test for queries with no null state.
 */
export function failedWithNoReading(q: { data: unknown; dataUpdatedAt: number; errorUpdatedAt: number }): boolean {
  return (q.data === undefined || q.data === null) && q.errorUpdatedAt > q.dataUpdatedAt
}
