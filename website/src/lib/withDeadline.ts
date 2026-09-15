/** An `Error` and not a `DOMException`, which the abort reason would otherwise be:
 *  the i18n gate exempts `Error` as a diagnostic callee, `DOMException` it reports. */
function timeoutReason(): Error {
  const e = new Error('deadline exceeded')
  e.name = 'TimeoutError'
  return e
}

/** Run `attempt` under a deadline, rejecting with TimeoutError if it has not
 *  settled in `ms`. Relays `outer` (react-query's unmount/cancel signal) and
 *  releases both the timer and the relay listener once settled.
 *
 *  Not `AbortSignal.timeout` + `AbortSignal.any`: the former exposes no handle
 *  so its timer cannot be released, and the latter's browser floor sits well
 *  above the rest of this codebase's. See the CR description. */
export function withDeadline<T>(
  ms: number,
  outer: AbortSignal | undefined,
  attempt: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  const ac = new AbortController()
  const timer = setTimeout(() => ac.abort(timeoutReason()), ms)
  const relay = () => ac.abort(outer?.reason)
  // A listener added to an already-aborted signal never fires, so we would
  // otherwise sit out the full deadline on an abandoned request.
  if (outer?.aborted) ac.abort(outer.reason)
  else outer?.addEventListener('abort', relay, { once: true })

  const release = () => {
    clearTimeout(timer)
    outer?.removeEventListener('abort', relay)
  }
  try {
    return attempt(ac.signal).finally(release)
  } catch (e) {
    release()   // a synchronous throw never reaches the `finally` above
    throw e
  }
}
