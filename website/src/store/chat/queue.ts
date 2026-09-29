/** The slot queue as transcript rows (`queued` bubbles): the one hydration path
 *  from a slot-detail `queue` field, and the push / pop / cancel / edit /
 *  reorder reducers the queue frames drive. */
import type { PayloadAction } from '@reduxjs/toolkit'
import type { ChatMessage } from '../../types'
import type { ChatState } from './state'
import { quoteBlock } from '../../chat-core/composer/messageQuote'
import { isUnsafeKey, queueEntryAttachments, queueEntryQuote, safeKey, type QueueEntryAttachments, type SlotQueueItem } from './wire'

/** SINGLE hydration path for the slot-detail `queue` field — the one place that
 *  turns backend queue entries into `queued` message bubbles. Every reducer that
 *  consumes a `fetchSlotDetail` payload (`switchSlot`, `warmSlotCache`,
 *  `refreshSlot`) routes through here so the hydration cannot be hand-copied and
 *  drift apart. Hand-copying it risks dropping queued messages: if `switchSlot`
 *  and `warmSlotCache` each mirror the same literal, a field added to one is
 *  silently forgotten in the other. Centralizing it means a new slot-detail
 *  payload field is added once and consumed everywhere.
 *
 *  Existing `queued` bubbles are stripped first so re-hydration is idempotent —
 *  a `queue_push` WS event may have appended a bubble during the HTTP fetch, and
 *  the server `queue` field is the canonical set. Returns a NEW array; queued
 *  bubbles are always appended last (after history), matching prior behavior. */
export function hydrateQueuedBubbles(
  list: ChatMessage[],
  queue: SlotQueueItem[] | undefined,
): ChatMessage[] {
  const base = list.filter((m) => m.role !== 'queued')
  for (const { content, queueId, ts, kind, appLabel, quote, ...attachments } of queue ?? []) {
    // The lists ride the row's meta under the same keys a user row carries
    // them, so a cancel on THIS tab restores a spaced path exactly even
    // though the send happened on another tab or before a reload.
    base.push({ role: 'queued', content, cls: 'msg msg-queued', ts, meta: { queueId, ...(kind ? { kind } : {}), ...(appLabel ? { appLabel } : {}), ...(quote ? { quote } : {}), ...attachments } })
  }
  return base
}

export const queueReducers = {
  /** Remove the first queued message matching content and append a user bubble at the end.
   *  The frame's `meta` (the entry's attachment lists, `files` / `dirs`) rides
   *  onto the rebuilt row: no `chat_message` echo follows for a user row, so
   *  this rebuild IS the row until the next reload, and without the lists the
   *  renderer resolves `[attached_file N]` markers by whitespace -- a spaced
   *  path (`/tmp/My Report.pdf`) truncates to `/tmp/My`. */
  removeQueuedMessage(state: ChatState, action: PayloadAction<{ slot: string; content: string; queue_id?: string; drain_writes_row?: boolean; meta?: Record<string, unknown> }>) {
    const { slot, content, queue_id, drain_writes_row, meta } = action.payload
    const msgs = slot === state.activeSlot ? state.messages : state.slotMessages[slot]
    if (!msgs) return
    const idx = queue_id
      ? msgs.findIndex(m => m.role === 'queued' && (m.meta?.queueId as string) === queue_id)
      : msgs.findIndex(m => m.role === 'queued' && m.content === content)
    if (idx >= 0) {
      const ts = msgs[idx].ts
      msgs.splice(idx, 1)
      // The DRAIN's own verdict: when it writes its own row (`inject` /
      // `subagent`) right after this pop, rebuilding the popped entry as a
      // `user` row shows the text twice, once attributed to the human. The
      // server computes this from the same classification the row write
      // uses, so a future system kind cannot be missed here — and an
      // EDITED cron card that drains as a real user row keeps its rebuild
      // (no chat_message echo follows for a user row).
      if (drain_writes_row) return
      msgs.push({ role: 'user', content, cls: 'msg msg-u', ts, ...(meta && Object.keys(meta).length ? { meta } : {}) })
      // Deliberately NO card retirement here. Three review rounds each found
      // a different way this path could retire the wrong card (system queue
      // items hydrated as indistinguishable rows; duplicate rows from the
      // hydration/queue_push race; a queued answer for card A landing after
      // a newer card B arrived). The server is the lifecycle owner:
      // the popped entry lands as a live user row there, which retires the
      // card's record and broadcasts `question_card_resolved` by identity to
      // every window, this one included.
    }
  },
  /** Cancel a queued message: remove from messages. pendingInput is set locally by the initiating client. */
  cancelQueuedMessage(state: ChatState, action: PayloadAction<{ slot: string; queue_id: string }>) {
    const { slot, queue_id } = action.payload
    const msgs = slot === state.activeSlot ? state.messages : state.slotMessages[slot]
    if (!msgs) return
    const idx = msgs.findIndex(m => m.role === 'queued' && (m.meta?.queueId as string) === queue_id)
    if (idx >= 0) msgs.splice(idx, 1)
  },
  /** Edit a queued message in place (from backend queue_edit WS event or optimistic local update). */
  /** Rewrite a queued row's text. `attachments` is the server's post-edit
   *  attachment lists (from the `queue_edit` frame's `meta`): when present it
   *  REPLACES the row's lists -- an edit that removes a marker prunes and
   *  renumbers the entry's lists on the server, so the pre-edit lists no
   *  longer index the renumbered markers and a later cancel would restore
   *  nothing from them. An empty object clears the lists (the frame carries
   *  no `meta` once every marker is gone). Omitted by the optimistic local
   *  edit, which cannot know how the server pruned them. */
  editQueuedMessage(state: ChatState, action: PayloadAction<{ slot: string; queue_id: string; content: string; attachments?: QueueEntryAttachments }>) {
    const { slot, queue_id, content, attachments } = action.payload
    if (isUnsafeKey(slot)) return
    const msgs = slot === state.activeSlot ? state.messages : state.slotMessages[slot]
    if (!msgs) return
    const idx = msgs.findIndex(m => m.role === 'queued' && (m.meta?.queueId as string) === queue_id)
    if (idx < 0) return
    msgs[idx].content = content
    if (attachments) {
      const { files: _f, dirs: _d, ...rest } = msgs[idx].meta ?? {}
      msgs[idx].meta = { ...rest, ...attachments }
    }
    // The server drops the entry's `meta.quote` when the edit takes its block
    // off the head of the text (`prune_quote_meta`); the same test here keeps
    // the row's record in step, so a later cancel restages no deleted quote.
    const { quote } = queueEntryQuote(msgs[idx].meta)
    if (msgs[idx].meta?.quote !== undefined && (!quote || !content.startsWith(quoteBlock(quote)))) {
      const { quote: _q, ...rest } = msgs[idx].meta ?? {}
      msgs[idx].meta = rest
    }
  },
  /** Reorder queued messages to match the given queue-id sequence (from the
   *  backend queue_reorder WS event or an optimistic local update). Queued
   *  messages are re-slotted in place - the positions they occupy in the
   *  message list stay fixed, only which queued message sits at each
   *  position changes. Ids missing from `order` keep their relative order
   *  after the ordered ones (mirrors the backend's semantics). */
  reorderQueuedMessages(state: ChatState, action: PayloadAction<{ slot: string; order: string[] }>) {
    const { slot, order } = action.payload
    if (isUnsafeKey(slot)) return
    const msgs = slot === state.activeSlot ? state.messages : state.slotMessages[slot]
    if (!msgs) return
    const queuedIdx: number[] = []
    msgs.forEach((m, i) => { if (m.role === 'queued' && (m.meta?.queueId as string)) queuedIdx.push(i) })
    if (queuedIdx.length < 2) return
    const byId = new Map(queuedIdx.map(i => [msgs[i].meta?.queueId as string, msgs[i]]))
    const ordered = order.filter(id => byId.has(id)).map(id => byId.get(id)!)
    const orderedSet = new Set(order)
    const remaining = queuedIdx.map(i => msgs[i]).filter(m => !orderedSet.has(m.meta?.queueId as string))
    const next = [...ordered, ...remaining]
    queuedIdx.forEach((msgIdx, k) => { msgs[msgIdx] = next[k] })
  },
  /** Add a queued message (from backend queue_push WS event). */
  appendQueuedMessage: {
    reducer(state: ChatState, action: PayloadAction<{ slot: string; content: string; ts: string; queueId: string; meta?: unknown }>) {
      const { slot, content, ts, queueId, meta } = action.payload
      const msgs = slot === state.activeSlot ? state.messages : (state.slotMessages[safeKey(slot)] ??= [])
      // A row with this queueId may ALREADY exist: slot-detail hydration
      // can land before a delayed `queue_push` for the same entry. Appending
      // blindly would duplicate the row; keep the existing one.
      if (msgs.some(m => m.role === 'queued' && (m.meta?.queueId as string) === queueId)) return
      // Same row shape as `hydrateQueuedBubbles`: the frame's attachment
      // lists ride the row so a cancel restores from them.
      msgs.push({ role: 'queued', content, cls: 'msg msg-queued', ts, meta: { queueId, ...queueEntryAttachments(meta), ...queueEntryQuote(meta) } })
    },
    prepare(payload: { slot: string; content: string; ts: string; queue_id?: string; meta?: unknown }) {
      return { payload: { ...payload, queueId: payload.queue_id || crypto.randomUUID() } }
    },
  },
}
