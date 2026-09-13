/** A bare rebuild must not pair a carried `rawSend` with content it was not captured against.
 *
 *  `hydrateQueuedBubbles` re-attaches `meta.rawSend` from the prior list keyed only on `queueId`,
 *  onto the SERVER's content. The `queue_edit` reducer drops `rawSend` on an edit it SEES, but a tab
 *  that missed that frame rebuilds a row whose content is post-edit while its carried payload is
 *  pre-edit -- and the cancel guard's `carried.sent === msg.content` equality is then the only thing
 *  refusing the stale restore. The carry is now conditional, so the pairing cannot drift at all. */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { setActiveSlot, appendMessage, appendQueuedMessage, refreshSlot } from './chatSlice'

const SLOT = 'slot-drift'
const SEND_ID = 's-drift'
const TYPED = 'the ORIGINAL typed text'

let QUEUE: unknown[] = []

vi.mock('../api/client', () => ({
  api: {
    chatSlotDetail: vi.fn(() => Promise.resolve({
      messages: [], running: false, stopping: false, has_more: false,
      total: 0, next_before: 0, queue: QUEUE,
    })),
  },
}))

type Row = { role: string; content: string; meta?: Record<string, unknown> }
type State = { chat: { messages: Row[] } }

describe('a bare rebuild must not carry a stale rawSend', () => {
  let store: ReturnType<typeof configureStore>
  // `appendQueuedMessage` assigns the queue id itself, so the test reads it back rather than
  // asserting one -- keying on a constant silently matched no row.
  let qid: string

  const queuedRow = () => (store.getState() as State).chat.messages.find(m => m.role === 'queued')

  beforeEach(() => {
    QUEUE = []
    store = configureStore({ reducer: { chat: chatReducer } })
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(appendMessage({
      role: 'user', content: TYPED, cls: '',
      meta: { sendId: SEND_ID, optimistic: true, pendingServerRow: true },
    } as never))
    store.dispatch(appendQueuedMessage({
      slot: SLOT, content: '[redacted]', ts: new Date().toISOString(), queueId: 'ignored', sendId: SEND_ID,
    }))
    qid = queuedRow()?.meta?.queueId as string
  })

  it('drops the carried payload when the rebuilt content no longer matches it', async () => {
    expect((queuedRow()?.meta?.rawSend as { text?: string } | undefined)?.text,
      'premise: the queued row carries the typed text').toBe(TYPED)

    // This tab never saw `queue_edit`, so the rebuild is the first time it learns the new content.
    QUEUE = [{ id: qid, content: 'text EDITED in another tab' }]
    await store.dispatch(refreshSlot(SLOT) as never)

    const rebuilt = queuedRow()
    expect(rebuilt?.content, 'premise: the rebuild took the server content').toBe('text EDITED in another tab')
    expect(rebuilt?.meta?.rawSend,
      'a pre-edit payload paired with post-edit text is the drift -- it must not survive the rebuild')
      .toBeUndefined()
  })

  it('keeps the carried payload when the content still matches', async () => {
    // Positive control: a carry hardwired to drop would satisfy the assertion above while breaking
    // the recovery this field exists for -- an unreadable receipt leaves no stash to fall back on.
    QUEUE = [{ id: qid, content: '[redacted]' }]
    await store.dispatch(refreshSlot(SLOT) as never)

    expect((queuedRow()?.meta?.rawSend as { text?: string } | undefined)?.text,
      'unchanged content means the payload still corresponds, so it must survive').toBe(TYPED)
  })
})
