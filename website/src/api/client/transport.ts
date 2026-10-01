/**
 * What every endpoint owner under `api/client/` is handed by the facade.
 *
 * The implementation stays in `api/client.ts`, which owns the whole transport
 * and auth-recovery pipeline: the `X-Session-Key` default, the five request
 * helpers, the `j` / `jNullable` / `jInstancesDisabled` parsers with their
 * `ApiError` journal, the session-expiry banner, silent refresh and the
 * stale-owner prompt. The domain
 * modules receive it through their `create*Endpoints` factory instead of
 * importing it, so none of them imports a runtime value from the facade that
 * composes them (`./telemetry` takes the Kiro usage types it defines, as types).
 *
 * The five helpers and the three parsers are the SAME function objects the facade
 * installs as the blessed `apiTransport`, so an edition's calls and core's calls
 * cannot diverge. The remaining members serve the methods that parse their own
 * response or talk raw `fetch`: they still have to reach the same recovery.
 *
 * `jInstancesDisabled` is the exception to that last sentence: it is NOT part of
 * the frozen `ApiTransport` edition seam, which carries `j`/`jNullable` only. It
 * exists here because one core endpoint owns one benign denial, and an edition
 * has no business opting out of a journal entry for a route it does not own.
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
  /**
   * `j` for THE ONE denial that is a designed, benign signal the caller handles
   * itself: `/api/instances` answering 403 with its own `instances_disabled`
   * code on an install where the control plane is off. The response still
   * THROWS its `ApiError`, so caller behaviour is unchanged, but it is not
   * written to the error journal and so cannot surface as a spurious error
   * report on whatever route happened to mount the caller.
   *
   * Pre-bound on purpose. This is not a `(status, code)` opt-out every module
   * can point at anything: the denial is baked in at the facade, so the only
   * thing a call site can do is use it or not. A second benign denial means a
   * second named member there, reviewed on its own merits.
   *
   * The match is on status AND the gateway's own `code`, never status alone —
   * that same endpoint answers 403 to a non-owner caller and to a Slack-origin
   * request, and keying on status would swallow both. The auth-recovery denials
   * can never be opted out.
   */
  jInstancesDisabled: (r: Response) => ReturnType<Response['json']>
  /** The shared `X-Session-Key: dashboard:ui` header, for a raw `fetch` that must still carry it. */
  sessionKeyHeader: { 'X-Session-Key': string }
  /** The pre-body 403 `X-Auth-Required` hook, for a method that reads its own response. */
  checkSessionExpired: (r: Response) => Response
  /** Clear the session-expired banner once a self-parsed response proved auth works. */
  removeAuthBanner: () => void
  /** `j`'s auth recovery for a response handed back RAW rather than parsed. */
  sendResponseAuthRecovery: (r: Response) => Response
  /** `withDeadline`, plus the journal: a read that hits its bound is recorded like an
   *  HTTP failure, and the report is pinned to the rejection so the notice that shows
   *  it hands the agent THIS read's endpoint, not whichever bounded read timed out last. */
  withJournaledDeadline: <T>(
    ms: number,
    outer: AbortSignal | undefined,
    endpoint: string,
    attempt: (signal: AbortSignal) => Promise<T>,
  ) => Promise<T>
}
