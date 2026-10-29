/**
 * ChatEmbed's transport for the shared Stop press protocol
 * (chat-core/stop/useStopPress): one press is one
 * `POST /api/chat/slots/{slot}/stop[?force=true]`, which the embed's existing
 * `/api/chat` grant already covers. The embed has no store, so it cannot use
 * ChatPane's Redux `requestStop` thunk; this maps the scoped API's reply onto
 * the protocol's outcomes instead.
 */
import type { AppApi } from './scopedApi'
import { ApiError, apiErrorCode } from '../api/apiError'
import type { StopOutcome } from '../chat-core/stop/useStopPress'

type StopReply = { ok?: boolean; compacting?: boolean; error?: string; code?: string } | null | undefined

export async function sendEmbedStop(api: Pick<AppApi, 'post'>, slotKey: string, force: boolean): Promise<StopOutcome> {
  const path = `/api/chat/slots/${encodeURIComponent(slotKey)}/stop${force ? '?force=true' : ''}`
  try {
    const reply = await api.post<StopReply>(path, {})
    // A 2xx can still carry a refusal (a peer-bound slot whose crew could not
    // be reached). It armed nothing, so the retry is a fresh press.
    if (reply && reply.ok === false) return { kind: 'refused', error: reply.error || reply.code || 'stop refused', code: reply.code }
    // `{ok, compacting}`: declined while automatic compaction holds the
    // session, and the backend armed its reset hatch for the next press.
    return reply?.compacting ? { kind: 'declined' } : { kind: 'ok' }
  } catch (err: unknown) {
    // No HTTP status means no answer: the press may have armed the cancel.
    const status = (err as { status?: unknown })?.status
    if (typeof status !== 'number') return { kind: 'lost' }
    // The status and the body's `code` name the reason in plain words
    // (stopRefusalCopy); the raw `API <status>: <body>` text is only a detail.
    return {
      kind: 'refused',
      error: err instanceof Error ? err.message : String(err),
      status,
      code: err instanceof ApiError ? apiErrorCode(err) : undefined,
      authRequired: err instanceof ApiError ? err.authRequired : undefined,
      edgeChallenge: err instanceof ApiError ? err.edgeChallenge : undefined,
    }
  }
}
