/**
 * Reply threads on a crewmate's chat messages (`dashboard/chat_threads.py`).
 *
 * A thread is the set of replies attached to ONE message of a crewmate's chat,
 * addressed by that message's durable `meta.mid`. Three reads/writes, all
 * through the blessed shared transport so failures carry an `ApiError` with
 * the backend's `code` (`parseErrorCode`) and land in the error journal.
 */

import { apiTransport } from './apiTransport'

const { get, post, j } = apiTransport

/** Footer data under one bubble: how many replies, when the last one landed, who took part. */
export interface ThreadSummary {
  count: number
  last_reply_ts: string
  /** Roles in first-appearance order, so the footer's faces read as the thread did. */
  participants: ('user' | 'assistant' | string)[]
}

export interface ThreadReply {
  id: string
  role: 'user' | 'assistant'
  content: string
  ts: string
}

export interface ThreadParent {
  mid: string
  role: 'user' | 'assistant' | string
  content: string
  ts: string
}

export interface ThreadDetail {
  parent: ThreadParent
  replies: ThreadReply[]
  /** The crewmate is still writing its reply in this thread. */
  in_flight: boolean
}

export const threadsQueryKey = (slot: string) => ['chat-threads', slot] as const
export const threadQueryKey = (slot: string, mid: string) => ['chat-thread', slot, mid] as const

export const threadsApi = {
  summary: (slot: string): Promise<{ threads: Record<string, ThreadSummary> }> =>
    get(`/api/chat/threads?slot=${encodeURIComponent(slot)}`).then(j) as Promise<{ threads: Record<string, ThreadSummary> }>,

  detail: (slot: string, mid: string): Promise<ThreadDetail> =>
    get(`/api/chat/threads/${encodeURIComponent(mid)}?slot=${encodeURIComponent(slot)}`).then(j) as Promise<ThreadDetail>,

  /** `replyId` (32 hex, minted per send) makes the send idempotent: re-sending
   *  the same id after a lost response returns the stored reply (`duplicate`)
   *  instead of storing it twice and running a second turn. */
  reply: (slot: string, mid: string, text: string, replyId?: string): Promise<{ reply: ThreadReply; run_id: string; duplicate?: boolean }> =>
    post(`/api/chat/threads/${encodeURIComponent(mid)}/reply`, { slot_key: slot, text, ...(replyId ? { reply_id: replyId } : {}) }).then(j) as Promise<{ reply: ThreadReply; run_id: string; duplicate?: boolean }>,
}
