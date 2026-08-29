/** a SUPERSEDED slot-detail response must not mutate the queue stash.
 *
 *  The adoption ran inside the plain fetch helper, which can see neither the store nor its own
 *  response's sequence, so a late-arriving older response still reached the stash -- and on an entry
 *  flagged `edited` that means DELETING a live queued-send record, which is the only copy of the
 *  attachments a redacted card was sent with. Cancel then has nothing to restore. */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { warmSlotCache } from './chatSlice'
import { queuedSendStash } from '../utils/queuedSendStash'

type Detail = { messages: unknown[]; running: boolean; stopping: boolean; has_more: boolean; total: number; queue: unknown[] }

const page = (queue: unknown[]): Detail => ({
  messages: [], running: false, stopping: false, has_more: false, total: 0, queue,
})

let pending: ((d: Detail) => void) | undefined
let nextMode: 'defer' | 'now' = 'now'
let nowQueue: unknown[] = []

vi.mock('../api/client', () => ({
  api: {
    chatSlotDetail: vi.fn(() => nextMode === 'defer'
      ? new Promise<Detail>(res => { pending = res })
      : Promise.resolve(page(nowQueue))),
  },
}))

describe('a superseded refetch must not delete a live queued-send record', () => {
  beforeEach(() => {
    queuedSendStash.clear()
    pending = undefined
    nextMode = 'now'
    nowQueue = []
  })

  const store = () => configureStore({ reducer: { chat: chatReducer } })

  it('leaves the record alone when a NEWER response already landed', async () => {
    // The only copy of this send's attachments: `raw` holds the user's text, `sent` the redacted
    // display form the card shows, and `files` the paths no parser can recover from the wire text.
    queuedSendStash.set('q-live', { raw: 'ship it @/tmp/My Report.pdf', files: ['/tmp/My Report.pdf'], sent: 'ship it' })
    const s = store()

    // An older fetch that has NOT resolved yet. Its page reports the entry as edited, which is the
    // arm that retires -- so if it is allowed to run late it destroys the record above.
    nextMode = 'defer'
    const stale = s.dispatch(warmSlotCache({ key: 'slot-sup' }) as never)

    // A newer fetch for the SAME slot resolves first, so its sequence is the one applied.
    nextMode = 'now'
    nowQueue = []
    await s.dispatch(warmSlotCache({ key: 'slot-sup' }) as never)

    // Now let the older one land.
    pending?.(page([{ id: 'q-live', content: 'ship it', edited: true }]))
    await stale

    expect(queuedSendStash.get('q-live')?.files,
      'a superseded response deleting this record loses the only copy of its attachments')
      .toEqual(['/tmp/My Report.pdf'])
  })

  it('still retires on a CURRENT response, so the guard is not a blanket skip', async () => {
    // Positive control: a fix that simply stopped retiring would satisfy the assertion above while
    // leaving a stale record to restore pre-edit text on Cancel.
    queuedSendStash.set('q-cur', { raw: 'the ORIGINAL text', files: [], sent: 'the ORIGINAL text' })
    nextMode = 'now'
    nowQueue = [{ id: 'q-cur', content: 'the EDITED text', edited: true }]

    await store().dispatch(warmSlotCache({ key: 'slot-cur' }) as never)

    expect(queuedSendStash.get('q-cur'),
      'a current response must still retire a record that predates the edit').toBeUndefined()
  })
})
