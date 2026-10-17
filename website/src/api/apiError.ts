/**
 * The typed API error, its message extraction, and a Response -> error factory.
 *
 * Split out of `api/client.ts` so an APP can import it. `client.ts` owns the
 * transport, and its module graph pulls in `queryClient`, `installApiTransport`,
 * artifact-write bookkeeping and the error journal — side effects a standalone
 * app has no business importing just to name an error type. Apps that already
 * import `client.ts` for other reasons are unaffected; the three that do not
 * (design-tweak, design-critique, mochi) stay independent of it.
 *
 * `client.ts` re-exports `ApiError` and `friendlyErrText`, so every existing
 * import path — and every test that mocks `../api/client` — keeps working.
 */
import { i18nT } from '../i18n/t'
import { looksLikeHtmlDocument, edgeChallengeMessage, noteEdgeAuthChallenge } from './edgeAuthChallenge'

/**
 * A failed API call, carrying the HTTP status so callers can branch on specific
 * codes (e.g. 404 = not found, 409 = conflict) without regex-matching the error
 * message text.
 *
 * Extends Error so existing `e instanceof Error ? e.message : String(e)`
 * fallbacks keep working.
 */
export class ApiError extends Error {
  readonly status: number
  /** The raw response body, kept so a caller can read structured fields that
   * `friendlyErrText` collapses away when it unwraps the human message. */
  readonly body: string
  /** The gateway rejected this call because the dashboard session no longer
   * authenticates (403 + `X-Auth-Required`). Call sites branch on this to drop
   * retry affordances that cannot succeed until the user re-authenticates. */
  readonly authRequired: boolean
  /** An interposed proxy answered with its own sign-in page, so no amount of
   * retrying reaches the gateway at all. Distinct from `authRequired`, which the
   * gateway's own 403 also sets and whose silent refresh recovers on retry. */
  readonly edgeChallenge: boolean
  constructor(
    status: number,
    message: string,
    body = '',
    authRequired = false,
    edgeChallenge = false,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
    this.authRequired = authRequired
    this.edgeChallenge = edgeChallenge
  }
}

/**
 * True for a rejection that means "the resource is absent" (HTTP 404), as
 * opposed to a request that FAILED. Duck-typed on `status` rather than
 * `instanceof ApiError` so a page that reads it keeps working under a mocked
 * `api/client` (the suites reject with `Object.assign(new Error(), { status })`),
 * and so any future ApiError-shaped rejection from a different transport counts.
 */
export const isNotFoundError = (e: unknown): boolean =>
  typeof e === 'object' && e !== null && (e as { status?: unknown }).status === 404

/**
 * The machine-readable `code` a refusal body carries, or `''` when the failure
 * is not an {@link ApiError} with a JSON object body naming one. Callers branch
 * on this instead of matching the human `error` text, which is prose and may be
 * localized or reworded.
 */
export const apiErrorCode = (e: unknown): string =>
  e instanceof ApiError ? errorBodyCode(e.body) : ''

/**
 * The `code` a raw refusal body names, or `''`. The body-level half of
 * {@link apiErrorCode}, for readers that run before an ApiError exists --
 * `friendlyErrText` builds the ApiError's message from the raw body.
 */
const errorBodyCode = (body: string): string => {
  try {
    const parsed = JSON.parse(body) as unknown
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return ''
    const code = (parsed as { code?: unknown }).code
    return typeof code === 'string' ? code : ''
  } catch {
    return ''
  }
}

/** True when an approval refusal is terminal — the approval itself is gone (404,
 *  or the endpoint's `no pending approval` 400). Duck-typed like {@link isNotFoundError}. */
export const isTerminalApprovalRefusal = (e: unknown): boolean => {
  if (typeof e !== 'object' || e === null) return false
  const r = e as { status?: unknown; message?: unknown; authRequired?: unknown }
  // 403 + re-auth leaves the approval live once the user signs back in.
  if (r.authRequired) return false
  return r.status === 404 || (r.status === 400 && r.message === 'no pending approval')
}

/**
 * Does this failure come from an interposed proxy's own sign-in page?
 *
 * Lives here rather than in `api/client` so `api/queryClient` can read it: client
 * imports queryClient to invalidate caches, so the reverse import would be a cycle.
 *
 * Narrower than the gateway's own `authRequired`, deliberately. That 403 also needs a
 * person, but there `attemptSilentRefresh` plus the one retry recovers a slept-laptop
 * cookie invisibly -- a successful refresh invalidates only `['auth-me']` and
 * `staleTime` is `Infinity`, so a query whose retry was cancelled holds an error card
 * until something else invalidates its key. A proxy challenge has no such recovery:
 * the request never reached the gateway.
 */
export const isEdgeChallengeError = (e: unknown): boolean =>
  e instanceof ApiError && e.edgeChallenge

/**
 * Is this a live-slot cap refusal? The gateway answers 429 with
 * `{"code":"slot_cap_reached"}` (global) or `creator_slot_cap_reached`
 * (per-caller) when a new session cannot open because too many are live.
 * Matches the parsed `code` EXACTLY, not a substring of the body, so an
 * unrelated 429 whose message happens to mention it is not misread.
 */
export const isSlotCapError = (status: number, body: string): boolean => {
  if (status !== 429) return false
  const code = errorBodyCode(body)
  return code === 'slot_cap_reached' || code === 'creator_slot_cap_reached'
}

/** The first of `keys` a JSON object body sets, when it is a non-blank string, else `''`. */
const jsonMessage = (trimmed: string, keys: readonly string[]): string => {
  if (!trimmed.startsWith('{')) return ''
  try {
    const parsed = JSON.parse(trimmed)
    const msg = keys.map((key) => parsed?.[key]).find((value) => value != null)
    if (typeof msg === 'string' && msg.trim()) return msg
  } catch { /* not JSON — the caller falls back */ }
  return ''
}

/**
 * Map raw edge/proxy error bodies to a human-readable message. A dashboard
 * served through Builder Tunnels sits behind API Gateway, whose throttle
 * response is the opaque `{"message":"Rate exceeded","throttlingReasons":null}`
 * — rendering that verbatim in an error card is a terrible UX. The mapped
 * message only ever shows after the QueryClient's 429 retry ladder
 * (api/queryClient.ts) is exhausted. A 429 the gateway sends itself names its
 * reason in `error` (a channel limit, a full queue), which the user is shown
 * instead; the edge throttle carries only `message`, so it never matches.
 *
 * Returns `''` for a body carrying no human message, which is the signal every
 * caller already turns into `HTTP <status>` — so that wording stays decided in
 * one place. The raw body remains on `ApiError.body` for diagnostics.
 */
export const friendlyErrText = (status: number, body: string): string => {
  const trimmed = body.trim()
  if (status === 429) {
    // Two different 429s reach here. The live-slot caps -- `slot_cap_reached`
    // (global) and `creator_slot_cap_reached` (per-caller) -- are hit by
    // Duplicate/Fork (each mints a new slot) once too many session tabs are
    // open; a generic "wait a few seconds and reload" is a dead end there,
    // because a reload re-restores the same open tabs straight back over the
    // cap, so the guidance names the one thing that clears it. Match the parsed
    // `code` EXACTLY, not a substring of the body, so an unrelated 429 whose
    // message happens to contain the text does not get the wrong copy. Any
    // other 429 the gateway sent names its reason in `error`; one with no reason,
    // like the tunnel edge's burst throttle, is a wait-and-retry.
    if (isSlotCapError(status, body)) {
      return i18nT('api.client.too_many_open_sessions_http_429_close_old')
    }
    return jsonMessage(trimmed, ['error', 'detail'])
      || i18nT('api.client.rate_limited_by_the_tunnel_edge_http_429_too_man')
  }
  // Backends return errors as {"error": "…"} (or detail/message). Unwrap the
  // field so the UI shows the human message with its real newlines, not the
  // raw JSON envelope with escaped \n and \".
  const msg = jsonMessage(trimmed, ['error', 'detail', 'message'])
  if (msg) return msg
  // An error PAGE has no message field to unwrap, so returning it verbatim put
  // `<!DOCTYPE html><html><head><meta charset="utf…` in the dashboard's topbar.
  if (looksLikeHtmlDocument(trimmed)) return ''
  return body
}

/**
 * Read a failed `Response` and build the {@link ApiError} for it.
 *
 * For app API clients, which each used to do
 * `throw new Error(body || \`HTTP ${status}\`)` — that put the raw wire body in
 * the message, so a refusal rendered as JSON in the UI and no caller could
 * branch on the status at all.
 *
 * The body is read ONCE and tolerates a read failure (`.catch`), because a
 * refusal mid-stream must still produce the status rather than an unrelated
 * throw. The header read is equally defensive (`?.`): this function runs on the
 * failure path, so anything it throws REPLACES the real error with a misleading
 * one — a `Response`-like object without `headers` would surface "Cannot read
 * properties of undefined" where the backend had said "path is outside the
 * allowed roots". The `HTTP <status>` fallback is what the previous bare-Error
 * calls used for an empty body, so an empty-body refusal reads exactly as it did
 * before.
 *
 * Deliberately does NOT journal to `utils/errorReport` and does NOT raise the
 * stale-owner prompt, both of which `client.ts::apiFailure` does on top of this.
 * Those are dashboard-session concerns tied to the dashboard's own error banner,
 * and they are the side effects this module exists to keep out of app bundles.
 * An app that wants them can call the dashboard client instead.
 */
export async function toApiError(r: Response): Promise<ApiError> {
  const body = await r.text().catch(() => '')
  const gatewayAuth = r.status === 403 && r.headers?.get?.('X-Auth-Required') === 'true'
  // The same refusal reaches app bundles through here, and their `/apps/<app>/api/…`
  // calls traverse the same proxy, so without this they print a bare `HTTP 403`.
  // Not consulted when the gateway's own header is present: that header proves the
  // gateway answered, and its HTML denial page would otherwise match.
  const edge = gatewayAuth
    ? null
    : noteEdgeAuthChallenge(r.status, r.headers?.get?.('content-type') ?? null, body)
  const message = edgeChallengeMessage(edge)
    || friendlyErrText(r.status, body)
    || `HTTP ${r.status}`
  // Every one of these needs a person: the gateway never saw the request, so a silent
  // retry a second later reproduces it whether a session lapsed or a firewall refused.
  const edgeChallenge = edge !== null
  return new ApiError(
    r.status,
    message,
    body,
    gatewayAuth || edgeChallenge,
    edgeChallenge,
  )
}

/**
 * A 2xx whose body could not be read -- an interrupted stream (`TypeError`) or
 * an unparseable body (`SyntaxError`). The server ACCEPTED the request; only
 * the receipt is lost. A plain body read lets these surface as the raw error,
 * where a caller cannot tell them from a request that never left (`fetch`
 * rejects with a `TypeError` too), so an endpoint whose 2xx is an acceptance
 * receipt (a chat turn) wraps post-2xx failures in this class: a caller that
 * treated "accepted, unreadable" as "unsent" would offer a retry that runs the
 * turn twice. Lives here, not in `client.ts`, so the app-sdk (its own fetch
 * helper, same hazard) and the many tests that mock `client.ts` wholesale can
 * share one identity for the `instanceof` check.
 */
export class AcceptedBodyUnreadable extends Error {
  /** `response` is where the unreadable 2xx came from (`headers`, `redirected`,
   *  final `url`). A send path hands it to `readSendReceipt`, so a login page an
   *  auth proxy wrote in the endpoint's place still reads as `refused` (nothing
   *  was sent; hand the text back) instead of `unknown`. */
  constructor(
    readonly reason: unknown,
    readonly response?: Pick<Response, 'headers' | 'redirected' | 'url'>,
  ) {
    // A human sentence, not blank: every scoped-API method of every App Kit app
    // reads through the tagging helper, and app code that surfaces
    // `err.message` from a malformed 2xx must not render an empty string.
    super(i18nT('api.client.accepted_body_unreadable') as string)
    this.name = 'AcceptedBodyUnreadable'
  }
}
