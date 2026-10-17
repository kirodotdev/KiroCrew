/**
 * ChatEmbed's transport for the shared Stop press protocol
 * (chat-core/stop/useStopPress): one press is one
 * `POST /api/chat/slots/{slot}/stop[?force=true]`, which the embed's existing
 * `/api/chat` grant already covers. The embed has no store, so it cannot use
 * ChatPane's Redux `requestStop` thunk; this maps the scoped API's reply onto
 * the protocol's outcomes instead.
 */
import type { AppApi } from './scopedApi'
import type { StopOutcome } from '../chat-core/stop/useStopPress'

type StopReply = { ok?: boolean; compacting?: boolean; error?: string; code?: string } | null | undefined

export async function sendEmbedStop(api: Pick<AppApi, 'post'>, slotKey: string, force: boolean): Promise<StopOutcome> {
  const path = `/api/chat/slots/${encodeURIComponent(slotKey)}/stop${force ? '?force=true' : ''}`
  try {
    const reply = await api.post<StopReply>(path, {})
    // A 2xx can still carry a refusal (a peer-bound slot whose crew could not
    // be reached). It armed nothing, so the retry is a fresh press.
    if (reply && reply.ok === false) return { kind: 'refused', error: reply.error || reply.code || 'stop refused' }
    // `{ok, compacting}`: declined while automatic compaction holds the
    // session, and the backend armed its reset hatch for the next press.
    return reply?.compacting ? { kind: 'declined' } : { kind: 'ok' }
  } catch (err: unknown) {
    // No HTTP status means no answer: the press may have armed the cancel.
    if (typeof (err as { status?: unknown })?.status !== 'number') return { kind: 'lost' }
    return { kind: 'refused', error: err instanceof Error ? err.message : String(err) }
  }
}
