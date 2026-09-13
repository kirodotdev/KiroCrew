/**
 * SCOPE BOUNDARY: a per-endpoint deadline is for a fetch the user waits on where an
 * unsettled promise is indistinguishable from an empty result AND the surface behind it
 * names a timeout apart from a failure in its own copy. That second half is the real
 * limit: a consumer rendering the server's message verbatim would surface this
 * rejection's untranslated `deadline exceeded` in every locale, trading a spinner for a
 * worse bug -- which is why the git panel and the knowledge search stay unbounded.
 *
 * Scope is stated as the CONSTANT set, not as a share of the client's `fetch('/api/...`
 * sites: that ratio was the argument for a general bound, but any unrelated edit to the API
 * client moves it, so it cannot be a rule. Scope is the four constants below, each carrying
 * its own rationale; an interim per-endpoint bound is a legal addition while the transport
 * default is pending. The general bound belongs
 * in the shared transport and cannot be blanket -- the long-poll endpoints and the streaming
 * reader must opt out -- and when it lands these constants are RETIRED. The replacement MUST
 * keep rejecting with a `TimeoutError`-named `Error`: `isDeadlineError` and `searchErrorCause`
 * key on that name, so a transport that rejects any other shape silently degrades every
 * cause-keyed notice added here back to the generic "failed" copy.
 *
 * RETRY is not uniform and that is deliberate: a listing takes one bounded attempt, since a retry
 * re-enters the bound and doubles it, while the @-menu search alone keeps the shared throttle
 * ladder so a 429 recovers instead of reading as an absence of matches.
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
