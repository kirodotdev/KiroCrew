/** What a slot-detail rebuild must not drop.
 *
 *  Two fields exist only to correlate an edit, and both are invisible until a rebuild happens:
 *  `editId` names WHICH edit produced an entry's text, and `editPrev` names the text a pending edit
 *  REPLACED. Losing either silently downgrades a correlation to a comparison of redacted display
 *  text, which redaction makes collide across clients.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

vi.mock('../api/client', () => ({ api: { chatSlotDetail: vi.fn() } }))

import chatReducer, { setActiveSlot, appendMessage, refreshSlot } from './chatSlice'
import { api } from '../api/client'
import { queuedSendStash, stashQueuedSend, noteLocalQueueEdit, settleQueueEditEcho } from '../utils/queuedSendStash'

const detail = vi.mocked(api.chatSlotDetail)
const SLOT = 'chat-7-1788030000'
const DISPLAY = 'look at an image'

const page = (queue: unknown[]) => ({
  messages: [], running: false, stopping: false, has_more: false, total: 0, next_before: 0, queue,
})

function makeStore() {
  return configureStore({
    reducer: { chat: chatReducer },
    middleware: (getDefault) => getDefault({ immutableCheck: false, serializableCheck: false }),
  })
}

describe('slot-detail normalization keeps the queue edit identity', () => {
  beforeEach(() => { queuedSendStash.clear(); detail.mockReset() })

  it('retires a record when the refetched entry names a DIFFERENT edit', async () => {
    stashQueuedSend('q9', { raw: 'look at @image.png', files: ['/tmp/image.png'], sent: 'look at @image.png' })
    noteLocalQueueEdit('q9', 'look at @image.png', 'edit-ours')
    expect(settleQueueEditEcho('q9', DISPLAY, 'edit-ours'),
      'premise: our own edit settled, so the record is settled for this display').toBe(true)

    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    // Another client's edit renders the SAME redacted display, so only the id can condemn it.
    detail.mockResolvedValue(page([{ id: 'q9', content: DISPLAY, edited: true, editId: 'edit-theirs' }]) as never)
    await store.dispatch(refreshSlot(SLOT))

    expect(queuedSendStash.get('q9'),
      'normalization dropping editId leaves a stale record to answer a cancel with obsolete files')
      .toBeUndefined()
  })

  it('keeps the record when the refetched entry names OUR edit', async () => {
    stashQueuedSend('q8', { raw: 'look at @image.png', files: ['/tmp/image.png'], sent: 'look at @image.png' })
    noteLocalQueueEdit('q8', 'look at @image.png', 'edit-ours')
    settleQueueEditEcho('q8', DISPLAY, 'edit-ours')

    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    detail.mockResolvedValue(page([{ id: 'q8', content: DISPLAY, edited: true, editId: 'edit-ours' }]) as never)
    await store.dispatch(refreshSlot(SLOT))

    expect(queuedSendStash.get('q8'),
      'the current edit own record is live recovery data').toBeDefined()
  })
})

describe('a queue rebuild carries the text a pending edit replaced', () => {
  beforeEach(() => { queuedSendStash.clear(); detail.mockReset() })

  it('keeps editPrev, so the stale-echo guard survives a refetch', async () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(appendMessage({
      role: 'queued', content: 'second text', cls: '', ts: '2026-09-11T13:00:00.000Z',
      meta: { queueId: 'q7', editPending: 'second text', editPrev: 'first text' },
    } as never))

    detail.mockResolvedValue(page([{ id: 'q7', content: 'second text', edited: true }]) as never)
    await store.dispatch(refreshSlot(SLOT))

    const row = store.getState().chat.messages.find(m => m.meta?.queueId === 'q7')
    expect(row?.meta?.editPending, 'premise: the pending edit still rides the rebuild').toBe('second text')
    expect(row?.meta?.editPrev,
      'without it a delayed echo of the replaced text cannot be told from real news')
      .toBe('first text')
  })
})
