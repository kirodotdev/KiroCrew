/**
 * Threads on a chat message (`dashboard/chat_threads.py`).
 *
 * A thread is an ordinary chat session plus an ANCHOR to one message. The
 * anchor is `(surface, conversation, mid)`; on the dashboard that is
 * `(dashboard, parent_slot_key, mid)`. A thread's own messages are the
 * transcript rows of its own slot, read through the ordinary chat surface —
 * so nothing here fetches or posts a thread's messages. What lives here is the
 * anchor: list the anchors on a parent's messages, read one, open a new one.
 *
 * Version 1 threads — the shipped feature, whose replies lived in a sidecar —
 * still announce themselves as `kind: "legacy"` rows, so the parent draws their
 * footer. Their replies are not served here: the read-only fold that renders
 * them ships separately, and the sidecar files are untouched (NOTES D3).
 */

import { apiTransport } from './apiTransport'

const { get, post, j } = apiTransport

/**
 * The `{mid}` path segment that means "the reply being written right now".
 *
 * A streaming reply has no `mid` at all: ids are minted when the row persists,
 * post-turn. The anchor for a thread opened on it is the user message that
 * started that turn (NOTES D4) — which the backend resolves and the UI cannot,
 * so the UI sends this sentinel instead of a mid. It can never collide with
 * one: a mid is `^m-[0-9a-f]{16}$`.
 */
export const ANCHOR_IN_FLIGHT = 'inflight'

/** Where a thread hangs off, channel-neutral (`messaging/link.py` + a message id). */
export interface ThreadAnchor {
  /** `dashboard`, `slack`, … — the channel identity used everywhere else. */
  surface: string
  /** The parent conversation: a dashboard slot key, a Slack channel id. */
  conversation: string
  mid: string
}

/** A live thread: an anchor pointing at the session that IS the thread. */
export interface SessionThreadSummary {
  kind: 'session'
  /** The thread's own slot — what the drawer and the full page both render. */
  thread_slot: string
  title: string
  /** `user` or `agent:<key>`. */
  opened_by: string
  opened_at: string
  /** Set once the thread was closed and its closing card posted. */
  closed_at: string | null
  /** The row in the parent conversation carrying the closing card. */
  summary_mid: string | null
}

/** A version 1 thread: replies in the sidecar, no session. Read-only. */
export interface LegacyThreadSummary {
  kind: 'legacy'
  count: number
  last_reply_ts: string
  /** Roles in first-appearance order, so the footer's faces read as it did. */
  participants: ('user' | 'assistant' | string)[]
}

/** One footer row under one bubble. The footer draws one badge per message and
 *  does not care which era the thread came from, so both kinds fold into one map. */
export type ThreadSummary = SessionThreadSummary | LegacyThreadSummary

export const isSessionThread = (s: ThreadSummary | undefined): s is SessionThreadSummary =>
  !!s && s.kind === 'session'
export const isLegacyThread = (s: ThreadSummary | undefined): s is LegacyThreadSummary =>
  !!s && s.kind === 'legacy'

/** One version 1 reply. Rendered read-only; nothing writes these any more. */
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
  /** The anchored message, quoted above the thread. */
  parent: ThreadParent
  /** The live thread on this message, or null when it has only version 1 replies. */
  anchor: SessionThreadSummary | null
}

/** What opening a thread hands back (201). */
export interface ThreadOpened {
  thread_slot: string
  anchor: ThreadAnchor
  title: string
  /** The seed message reached the thread. False = the thread exists but opened
   *  empty, which is a thread the user can still type into. */
  seeded: boolean
}

/** What ending a thread hands back. */
export interface ThreadClosed {
  thread_slot: string
  anchor: ThreadAnchor
  /** The closing card's row in the parent, or null when the card did not post.
   *  The thread is closed either way: the card is published after the durable
   *  transition, so the only surviving failure costs the back-link. */
  summary_mid: string | null
}

export const threadsQueryKey = (slot: string) => ['chat-threads', slot] as const
export const threadQueryKey = (slot: string, mid: string) => ['chat-thread', slot, mid] as const

export const threadsApi = {
  summary: (slot: string): Promise<{ threads: Record<string, ThreadSummary> }> =>
    get(`/api/chat/threads?slot=${encodeURIComponent(slot)}`).then(j) as Promise<{ threads: Record<string, ThreadSummary> }>,

  detail: (slot: string, mid: string): Promise<ThreadDetail> =>
    get(`/api/chat/threads/${encodeURIComponent(mid)}?slot=${encodeURIComponent(slot)}`).then(j) as Promise<ThreadDetail>,

  /**
   * Open a thread on one message and get its slot back.
   *
   * `mid` is the anchored message, or `ANCHOR_IN_FLIGHT` when the row is still
   * streaming. Opening never addresses the parent's running turn — it mints a
   * sibling session — so it never refuses because the parent is busy (NOTES D5).
   *
   * `title` is the only option this client sends. The route also takes `agent` and
   * `note`, which the MCP `thread_open` tool fills; no surface here composes either,
   * so carrying them would be a parameter nothing on this side of the wire sets.
   */
  open: (slot: string, mid: string, opts?: { title?: string }): Promise<ThreadOpened> =>
    post(`/api/chat/threads/${encodeURIComponent(mid)}/open`, {
      slot_key: slot,
      ...(opts?.title ? { title: opts.title } : {}),
    }).then(j) as Promise<ThreadOpened>,

  /**
   * End the thread on one message: mark the anchor closed and post the closing
   * card back in the parent.
   *
   * Distinct from dismissing the drawer, which is a view action and reaches
   * nothing. Ending is what releases the anchor, so the message can carry a new
   * thread later, and what returns the thread's result to the conversation it
   * came from. The thread's own session is untouched and stays readable.
   *
   * `threadSlot` is the thread the caller BELIEVES it is ending, and the backend
   * refuses when the anchor names a different one. Without it a drawer left open
   * while the message was closed and reopened elsewhere would end the replacement:
   * the mid alone says which message, never which thread.
   */
  close: (slot: string, mid: string, threadSlot: string): Promise<ThreadClosed> =>
    post(`/api/chat/threads/${encodeURIComponent(mid)}/close`, { slot_key: slot, thread_slot: threadSlot }).then(j) as Promise<ThreadClosed>,
}
