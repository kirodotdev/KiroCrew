/** The slot queue as transcript rows (`queued` bubbles): the one hydration path
 *  from a slot-detail `queue` field, and the push / pop / cancel / edit /
 *  reorder reducers the queue frames drive. */
import type { PayloadAction } from '@reduxjs/toolkit'
import type { ChatMessage } from '../../types'
import type { ChatState } from './state'
import { quoteBlock } from '../../chat-core/composer/messageQuote'
import { isUnsafeKey, queueEntryAttachments, queueEntryPastes, queueEntryQuote, queueEntrySendId, safeKey, type QueueEntryAttachments, type SlotQueueItem } from './wire'
import type { PasteBlock } from '../../utils/pasteTokens'

/** A queued row's paste blocks (`queueEntryPastes` put them there): what the
 *  user row the entry drains into receives. */
function queuedRowPastes(meta: Record<string, unknown> | undefined): { pastes?: PasteBlock[] } {
  const pastes = meta?.pastes
  return Array.isArray(pastes) && pastes.length ? { pastes: pastes as PasteBlock[] } : {}
}

/** Replace only this send's provisional row; a queue acceptance is not execution.
 *
 *  A crewmate chat's own send (`responsiveSend`) is replaced IN PLACE: the
 *  queued row takes the bubble's index, ts and key (`meta.clientTs`) and its
 *  `responsiveSend` mark, so the chat that draws the
 *  queued row as the person's bubble (`withQueuedAsBubbles`) shows one stable
 *  bubble where it was sent -- no removal at the bottom and redraw a frame
 *  later, and no jump below rows that arrived after it. Every other provisional
 *  row is spliced and the queued row appended, as before. */
function appendCorrelatedQueue(list: ChatMessage[], row: ChatMessage): void {
  const sendId = row.meta?.sendId
  if (sendId) {
    // A late push cannot resurrect a queue card whose pop already ran locally.
    if (list.some(m => m.role === 'user' && !m.meta?.optimistic
      && m.meta?.sendId === sendId && m.meta?.queueId === row.meta?.queueId)) return
    for (let i = list.length - 1; i >= 0; i--) {
      const m = list[i]
      if (m.role !== 'user' || !m.meta?.optimistic || m.meta?.sendId !== sendId) continue
      if (m.meta?.responsiveSend && !list.some(q => q.role === 'queued' && q.meta?.queueId === row.meta?.queueId)) {
        // The bubble's own ts too: the pane's list keys a row by its ts.
        const clientTs = (m.meta?.clientTs as string | undefined) || m.ts
        list[i] = { ...row, ts: m.ts ?? row.ts, meta: { ...row.meta, responsiveSend: true, ...(clientTs ? { clientTs } : {}) } }
        return
      }
      list.splice(i, 1)
    }
  }
  if (!list.some(m => m.role === 'queued' && m.meta?.queueId === row.meta?.queueId)) list.push(row)
}

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
 *  bubbles are appended last (after history), matching prior behavior, except
 *  a crewmate send's queued row, which keeps the place it was sent. */
export function hydrateQueuedBubbles(
  list: ChatMessage[],
  queue: SlotQueueItem[] | undefined,
  preserveLiveQueue = false,
): ChatMessage[] {
  // A first pane hydrate fills gaps; frames already applied locally are newer
  // than its snapshot. Other fetch paths retain their authoritative semantics.
  // A crewmate send's queued row stays where it was sent (see
  // appendCorrelatedQueue) while the server still lists it: stripping and
  // re-appending it would move the person's bubble to the bottom and re-key it.
  const listed = new Set((queue ?? []).map(q => q.queueId))
  const pinned = (m: ChatMessage) => m.role === 'queued' && !!m.meta?.responsiveSend
    && (preserveLiveQueue || listed.has(m.meta?.queueId as string))
  const live = preserveLiveQueue ? list.filter(m => m.role === 'queued' && !pinned(m)) : []
  const base = list.filter((m) => m.role !== 'queued' || pinned(m))
  const liveIds = new Set([...live, ...base.filter(m => m.role === 'queued')].map(m => m.meta?.queueId))
  for (const { content, queueId, ts, kind, appLabel, quote, ...attachments } of queue ?? []) {
    if (liveIds.has(queueId)) continue
    // The lists ride the row's meta under the same keys a user row carries
    // them, so a cancel on THIS tab restores a spaced path exactly even
    // though the send happened on another tab or before a reload. The paste
    // blocks ride the same way, so the card draws the `[ Paste #N ]` token
    // the sent bubble draws, not the expanded wire text.
    appendCorrelatedQueue(base, { role: 'queued', content, cls: 'msg msg-queued', ts, meta: { queueId, ...(kind ? { kind } : {}), ...(appLabel ? { appLabel } : {}), ...(quote ? { quote } : {}), ...attachments, ...queueEntryPastes(content) } })
  }
  for (const row of live) appendCorrelatedQueue(base, row)
  return base
}

/** A pop/cancel can precede the first message array as well as the HTTP read. */
function retireBeforePaneHydrate(state: ChatState, slot: string, queueId: string | undefined): void {
  if (!queueId || slot === state.activeSlot || state.slotHydrated?.[slot]
    || state.slotPaneHasMore?.[slot] !== undefined) return
  const ids = ((state.slotPaneRemovedQueueIds ??= {})[safeKey(slot)] ??= [])
  if (!ids.includes(queueId)) ids.push(queueId)
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
    if (isUnsafeKey(slot)) return
    retireBeforePaneHydrate(state, slot, queue_id)
    const sendId = queueEntrySendId(meta).sendId
    let msgs = slot === state.activeSlot ? state.messages : state.slotMessages[safeKey(slot)]
    if (!msgs) {
      // Only a send-id pop writes a row into a pane with no transcript yet; any
      // other pop must not leave an empty array that reads as a loaded pane.
      if (!sendId || drain_writes_row) return
      msgs = state.slotMessages[safeKey(slot)] = []
    }
    const idx = queue_id
      ? msgs.findIndex(m => m.role === 'queued' && (m.meta?.queueId as string) === queue_id)
      : msgs.findIndex(m => m.role === 'queued' && m.content === content)
    if (idx >= 0 || sendId) {
      const queued = idx >= 0 ? msgs[idx] : undefined
      let ts = queued?.ts
      // A crewmate send drains where it was sent (its queued row or its
      // still-provisional bubble holds that place), keeping the bubble's key,
      // so the person's message neither jumps to the bottom nor remounts. Every
      // other drained entry is the turn's opener and lands at the end.
      let at = -1
      let clientTs = queued?.meta?.responsiveSend ? queued.meta.clientTs as string | undefined : undefined
      if (idx >= 0) {
        if (queued?.meta?.responsiveSend) at = idx
        msgs.splice(idx, 1)
      }
      // The DRAIN's own verdict: when it writes its own row (`inject` /
      // `subagent`) right after this pop, rebuilding the popped entry as a
      // `user` row shows the text twice, once attributed to the human. The
      // server computes this from the same classification the row write
      // uses, so a future system kind cannot be missed here — and an
      // EDITED cron card that drains as a real user row keeps its rebuild
      // (no chat_message echo follows for a user row).
      if (drain_writes_row) return
      if (sendId) {
        const existing = msgs.find(m => m.role === 'user' && m.meta?.sendId === sendId)
        if (existing && !existing.meta?.optimistic) return
        if (existing) {
          const ei = msgs.indexOf(existing)
          if (existing.meta?.responsiveSend && at < 0) {
            at = ei
            clientTs = (existing.meta?.clientTs as string | undefined) || existing.ts
            ts ??= existing.ts
          } else if (at > ei) at -= 1
          msgs.splice(ei, 1)
        }
      }
      // The queued row's paste blocks go with the text: the user row renders
      // them as the chip at once (`recollapsePastes` on render) instead of
      // showing the expanded paste until the next reconcile finds the blocks.
      const rowMeta: Record<string, unknown> = { ...queueEntryAttachments(queued?.meta), ...queueEntryQuote(queued?.meta), ...queuedRowPastes(queued?.meta), ...queueEntryAttachments(meta), ...queueEntryQuote(meta), ...queueEntrySendId(meta) }
      if (queue_id && sendId) rowMeta.queueId = queue_id
      if (at >= 0 && clientTs) rowMeta.clientTs = clientTs
      delete rowMeta.optimistic
      delete rowMeta.deliveryUnconfirmed
      delete rowMeta.responsiveSend
      const row: ChatMessage = { role: 'user', content, cls: 'msg msg-u', ts, ...(Object.keys(rowMeta).length ? { meta: rowMeta } : {}) }
      if (at >= 0) msgs.splice(at, 0, row)
      else msgs.push(row)
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
    if (isUnsafeKey(slot)) return
    retireBeforePaneHydrate(state, slot, queue_id)
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
  editQueuedMessage(state: ChatState, action: PayloadAction<{ slot: string; queue_id: string; content: string; attachments?: QueueEntryAttachments; pastes?: PasteBlock[] }>) {
    const { slot, queue_id, content, attachments, pastes } = action.payload
    if (isUnsafeKey(slot)) return
    const msgs = slot === state.activeSlot ? state.messages : state.slotMessages[slot]
    if (!msgs) return
    const idx = msgs.findIndex(m => m.role === 'queued' && (m.meta?.queueId as string) === queue_id)
    if (idx < 0) return
    msgs[idx].content = content
    // The card's commit names the blocks whose tokens the edit kept, in token
    // order (`pastes`); the row takes exactly that set. Named, not inferred
    // from the text: a deleted body that is a prefix of a kept sibling's still
    // occurs inside it, and a text test would keep the deleted block. The
    // echo frame and another tab's edit carry no set and leave the row's
    // blocks as they are: a renumbered attachment marker in the echo leaves
    // the pastes in it unchanged, and a token another tab removed leaves a
    // block whose body the card's fold no longer finds, so that card shows
    // the text as sent rather than a wrong token.
    if (pastes) {
      const { pastes: _p, ...rest } = msgs[idx].meta ?? {}
      msgs[idx].meta = { ...rest, ...(pastes.length ? { pastes } : {}) }
    }
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
      // Same row shape as `hydrateQueuedBubbles`: the frame's attachment
      // lists ride the row so a cancel restores from them, and the paste
      // blocks so the card shows the token.
      appendCorrelatedQueue(msgs, { role: 'queued', content, cls: 'msg msg-queued', ts, meta: { queueId, ...queueEntryAttachments(meta), ...queueEntryQuote(meta), ...queueEntrySendId(meta), ...queueEntryPastes(content) } })
    },
    prepare(payload: { slot: string; content: string; ts: string; queue_id?: string; meta?: unknown }) {
      return { payload: { ...payload, queueId: payload.queue_id || crypto.randomUUID() } }
    },
  },
}
