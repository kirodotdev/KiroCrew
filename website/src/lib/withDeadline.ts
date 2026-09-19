/**
 * SCOPE BOUNDARY: a per-endpoint deadline is for a fetch the user waits on where an
 * unsettled promise is indistinguishable from an empty result AND the surface behind it
 * names a timeout apart from a failure in its own copy. A consumer that renders the
 * server message verbatim would surface this rejection's untranslated `deadline
 * exceeded` in every locale, which is why the git panel and knowledge search stay
 * unbounded.
 *
 * Scope is the constant set in the API client, not a share of its `fetch` sites. A
 * general bound belongs in the shared transport, and when it lands these are RETIRED.
 * The replacement MUST keep rejecting with a `TimeoutError`-named `Error`:
 * `isDeadlineError` and `searchErrorCause` key on that name, so a transport rejecting
 * any other shape silently degrades every cause-keyed notice to the generic copy.
 */

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
