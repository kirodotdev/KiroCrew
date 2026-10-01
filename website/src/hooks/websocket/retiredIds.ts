/** Watermarked logs of identities retired by live frames.
 *
 *  A reconnect snapshot races the live stream: an approval or question card
 *  retired while its HTTP read is in flight can still be listed in the answer.
 *  Each owner records retirements here and reads back the ones after the
 *  watermark it took before the request, so a stale snapshot row is never
 *  revived. Shared by the approval registry and the question reconcile. */

/** Ask-ids recorded as resolved after `watermark`, newest-agnostic order.
 *
 *  The log is keyed by ask_id with a monotonic sequence rather than an array,
 *  so bounding it cannot shift the watermark's meaning. */
export function resolvedSince(log: Map<string, number>, watermark: number): string[] {
  const out: string[] = []
  for (const [askId, seq] of log) if (seq > watermark) out.push(askId)
  return out
}

/** Record an identity in a bounded monotonic log without shifting watermark meaning. */
export function recordInBoundedLog(
  log: Map<string, number>,
  sequence: { current: number },
  id: string,
): void {
  if (!id) return
  log.set(id, ++sequence.current)
  if (log.size > 200) {
    // Drop the oldest entries; a reconcile only ever consults recent ones.
    const oldest = [...log.entries()].sort((a, b) => a[1] - b[1]).slice(0, log.size - 200)
    for (const [retiredId] of oldest) log.delete(retiredId)
  }
}
