/**
 * Stale-origin CSRF-refusal signal — shared, dependency-free detection.
 *
 * The gateway's CSRF barrier (`dashboard/server.py::_make_csrf_middleware`)
 * raises `web.HTTPForbidden(text="CSRF check failed: request origin not
 * allowed.")` on any state-mutating request whose `Origin`/`Referer` is not in
 * the gateway's allowlist and does not same-origin-match its `Host`. That is a
 * bare **403 with NO `X-Auth-Required` header** and a plain-text body — so it is
 * NOT the auth-expiry 403 (which `sessionExpirySignal` handles via silent
 * refresh) and NOT an interposed proxy's HTML page (which `edgeAuthChallenge`
 * handles). Left unclassified it falls through `apiFailure` to a generic error
 * card, which is exactly the "New chat 403s repeatably until a full refresh"
 * bug: a still-open tab keeps sending an `Origin` that no longer matches the
 * gateway's current host:port (the classic trigger is a gateway restart that
 * moved the port), and only a full reload re-bootstraps the SPA under the
 * gateway's current origin so the barrier passes again.
 *
 * The recovery is a full-document reload — the SAME action the user already
 * discovered by hand — done automatically, once, behind a visible notice. This
 * module is a LEAF on purpose (mirrors `sessionExpirySignal`/`staleOwnerSignal`):
 * the direct-fetch surfaces (app-sdk, MCP-app relay) import only this file, not
 * the whole `api/client` graph. `api/client` installs the actual reload/notice
 * handler at its own module load; in a document where it never loads, detection
 * still returns true and the caller keeps its own error path.
 */

/**
 * The exact plain-text body the CSRF barrier raises. Anchored on this rather
 * than a loose "403" so an unrelated 403 (a genuine permission denial that also
 * lacks `X-Auth-Required`) is never mistaken for a stale-origin condition.
 * Substring-matched, not equality: aiohttp may append the status line to the
 * body, and a future revision may add detail after the sentence.
 */
export const CSRF_ORIGIN_REFUSAL_MARKER = 'request origin not allowed'

type CsrfOriginHandler = () => void

let _handler: CsrfOriginHandler | null = null

/** Installed once by `api/client` (the module that owns the reload + notice). */
export function installCsrfOriginHandler(handler: CsrfOriginHandler): void {
  _handler = handler
}

/** Test-only: detach the handler so cases can assert the uninstalled no-op. */
export function __resetCsrfOriginHandlerForTests(): void {
  _handler = null
}

/**
 * Detect the gateway's CSRF stale-origin refusal on a failed response and fire
 * the installed recovery handler.
 *
 * All three signals are required, and each rules out a different look-alike:
 *  - `status === 403` — the barrier's status;
 *  - `!authRequired` — the gateway's auth-expiry 403 carries `X-Auth-Required:
 *    true`; that one is recoverable by silent refresh and must keep its own
 *    path, never a document reload;
 *  - body contains {@link CSRF_ORIGIN_REFUSAL_MARKER} — the barrier's own text,
 *    which distinguishes it from any other headerless 403.
 *
 * Returns whether the signal matched. Like the sibling signals it only ADDS the
 * recovery; the caller's `ApiError` is still thrown, so a reload that is
 * suppressed by the loop guard degrades to the actionable error message rather
 * than swallowing the failure.
 */
export function noteCsrfOriginResponse(
  status: number,
  authRequired: boolean,
  body: string,
): boolean {
  if (status !== 403 || authRequired) return false
  if (!body.includes(CSRF_ORIGIN_REFUSAL_MARKER)) return false
  _handler?.()
  return true
}
