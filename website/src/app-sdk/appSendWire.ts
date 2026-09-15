/**
 * The chat-core transport's wire for an app-sdk embed.
 *
 * `sendTurn` classifies a send through the shared `readSendReceipt` reader,
 * which wants the fetch seam's shape: a response that RESOLVES on every HTTP
 * status and rejects only when the request never went out. The scoped
 * `AppApi` an app is handed does not look like that -- its JSON helper throws
 * on non-2xx and returns the parsed body on 2xx -- and it is the ONLY client
 * an embed may use, because it is what enforces the host app's declared
 * `allowedApiPaths` (an embed's send must stay a permission the app grants).
 *
 * This adapter re-expresses the scoped helper's outcomes in the seam's shape,
 * so an embed's receipt is read by the same classifier as every other
 * surface's:
 *
 * - 2xx with a JSON body      -> resolved `{ ok: true }` whose `json()` yields
 *                                the body (`dispatched` / `queued` / a
 *                                readable refusal, by the shared rule).
 * - 2xx, body unreadable      -> resolved `{ ok: true }` whose `json()`
 *   (stream cut or non-JSON)     rejects, i.e. `unknown`. The helper tags a
 *                                post-2xx failure `AcceptedBodyUnreadable`, so
 *                                it is never mistaken for a request that never
 *                                left (also a `TypeError`); the old embed send
 *                                swallowed the non-JSON case as success.
 * - non-2xx                   -> resolved `{ ok: false }` carrying the body
 *                                text, i.e. a `refused` receipt that keeps the
 *                                server's own reason.
 * - permission denied         -> resolved `{ ok: false }` whose body carries a
 *                                human sentence ("<app> isn't allowed to send
 *                                chat messages -- it doesn't declare chat
 *                                access in its permissions"), i.e. `refused` with a
 *                                reason. Nothing left the document, so the
 *                                payload is safe to hand back -- but "check
 *                                your connection" would be advice that can
 *                                never succeed, so this is not a transport
 *                                error. The developer detail (app name, path,
 *                                declared grants) goes to the console, where
 *                                the person who can act on it is.
 * - fetch itself rejected     -> rejected with `AbortError`, i.e.
 *                                `response-late`. A raw network rejection
 *                                cannot tell "never left" (offline, DNS) from
 *                                "the server took the POST and the connection
 *                                reset before headers" -- and this is a single
 *                                POST with no earlier call on the same link to
 *                                weigh the odds. Indeterminate, so it must NOT
 *                                be the retry-safe `transport-error`: the embed
 *                                hands the text back under the unconfirmed
 *                                notice and the poll's `sendId` echo retires it
 *                                if the turn did run, instead of inviting a
 *                                duplicate turn.
 * - `signal` fired            -> rejected with `AbortError`, i.e.
 *                                `response-late`. The scoped helper takes no
 *                                signal, so the underlying fetch is not
 *                                cancelled -- the deadline is honoured at the
 *                                receipt, which is the contract that matters.
 *
 * `?ws=1` selects the JSON receipt instead of the SSE stream the bare
 * endpoint answers with. The scoped path check reads only the pathname and
 * passes the query through, so this needs no new permission from the app.
 */
import type { AppApi } from './index'
import { AppApiPermissionError } from './apiError'
import { AcceptedBodyUnreadable, ApiError } from '../api/apiError'
import { indeterminate, settleUnderSignal, type SendWire } from '../chat-core/transport/sendTurn'
import type { SendResponseLike } from '../utils/sendDelivery'
import { i18nT } from '../i18n/t'

const APP_SEND_PATH = '/api/chat?ws=1'

/** `agent` is the agent the generic endpoint binds when it has to CREATE the
 *  slot. Part of the wire, not of the transport contract: only this surface
 *  sends it. */
export function appApiSendWire(api: AppApi, agent?: string): SendWire {
  return (payload, signal) => settleUnderSignal<SendResponseLike>(signal, () =>
    api.post<unknown>(APP_SEND_PATH, {
      message: payload.message,
      slot: payload.slot,
      agent: agent || '',
      ...(payload.meta ? { meta: payload.meta } : {}),
    }).then(
      (parsed: unknown): SendResponseLike => ({ ok: true, json: () => Promise.resolve(parsed) }),
      (err: unknown): SendResponseLike => {
        if (err instanceof ApiError) {
          const { status, body: bodyText } = err
          // A refusal whose body is not the server's JSON envelope (a proxy 502
          // page, an aiohttp HTML 500, a bodyless 413) still carries a reason
          // the row can state -- the status -- instead of falling through to
          // the transport's reason-less "check your connection" copy, which is
          // wrong advice for a server that answered.
          return {
            ok: false,
            json: () => Promise.resolve().then(() => {
              try { return JSON.parse(bodyText) as unknown }
              catch { return { error: i18nT('appSdk.chatEmbed.server_refused_with_status', { status }) } }
            }),
          }
        }
        // 2xx received, body lost or unparseable: accepted, receipt unreadable.
        if (err instanceof AcceptedBodyUnreadable) return { ok: true, json: () => Promise.reject(err.reason) }
        if (err instanceof AppApiPermissionError) {
          // eslint-disable-next-line no-console -- developer detail for the app author; the row carries the human sentence
          console.warn(err.message)
          // The row names the app and says where the grant lives (the app's
          // declared permissions) so "this app" is not a dead end.
          const error = i18nT('appSdk.chatEmbed.app_not_allowed_to_send_named', { app: err.appName }) as string
          return { ok: false, json: () => Promise.resolve({ error }) }
        }
        // Anything else is the fetch itself rejecting. The scoped helper has
        // already turned every response the server DID send into one of the
        // three shapes above, so what is left is a request with no response:
        // never left, or accepted and then the connection dropped before
        // headers. Nothing here can tell the two apart, and the second one
        // means a retry runs the turn twice. Indeterminate -> `response-late`.
        throw indeterminate()
      },
    ))
}
