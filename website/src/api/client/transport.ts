/**
 * What every endpoint owner under `api/client/` is handed by the facade.
 *
 * The implementation stays in `api/client.ts`, which owns the whole transport
 * and auth-recovery pipeline: the `X-Session-Key` default, the five request
 * helpers, the `j`/`jNullable` parsers with their `ApiError` journal, the
 * session-expiry banner, silent refresh and the stale-owner prompt. The domain
 * modules receive it through their `create*Endpoints` factory instead of
 * importing it, so none of them imports a runtime value from the facade that
 * composes them (`./telemetry` takes the Kiro usage types it defines, as types).
 *
 * The five helpers and the two parsers are the SAME function objects the facade
 * installs as the blessed `apiTransport`, so an edition's calls and core's calls
 * cannot diverge. The remaining members serve the methods that parse their own
 * response or talk raw `fetch`: they still have to reach the same recovery.
 */
export interface ClientTransport {
  get: (url: string, sessionKey?: string, signal?: AbortSignal) => Promise<Response>
  post: (
    url: string,
    body?: object,
    sessionKey?: string,
    extra?: HeadersInit,
    redirect?: RequestRedirect,
  ) => Promise<Response>
  put: (url: string, body: object, sessionKey?: string, extra?: HeadersInit) => Promise<Response>
  del: (url: string, body?: object, sessionKey?: string, extra?: HeadersInit) => Promise<Response>
  patch: (url: string, body: object, sessionKey?: string, signal?: AbortSignal) => Promise<Response>
  /** Parse a 2xx body; run auth recovery and throw a journaled `ApiError` otherwise. */
  j: (r: Response) => ReturnType<Response['json']>
  /** `j`, except that a 204 resolves to `null`. */
  jNullable: (r: Response) => ReturnType<Response['json']>
  /** The shared `X-Session-Key: dashboard:ui` header, for a raw `fetch` that must still carry it. */
  sessionKeyHeader: { 'X-Session-Key': string }
  /** The pre-body 403 `X-Auth-Required` hook, for a method that reads its own response. */
  checkSessionExpired: (r: Response) => Response
  /** Clear the session-expired banner once a self-parsed response proved auth works. */
  removeAuthBanner: () => void
  /** `j`'s auth recovery for a response handed back RAW rather than parsed. */
  sendResponseAuthRecovery: (r: Response) => Response
}
