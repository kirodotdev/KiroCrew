/** An EDITED queued entry must not adopt its pre-send record.
 *
 *  A missed `queue_push` makes slot-detail hydration the only adoption point, and adoption keeps
 *  the record's own raw/files while taking only `sent` from the fetched content -- so on an entry
 *  edited elsewhere the cancel guard passes and Cancel restores the PRE-EDIT text and files. */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { warmSlotCache } from './chatSlice'
import { preSendStash, queuedSendStash, stashPreSend } from '../utils/queuedSendStash'

let QUEUE: unknown[] = []

vi.mock('../api/client', () => ({
  api: {
    chatSlotDetail: vi.fn(() => Promise.resolve({
      messages: [], running: false, stopping: false, has_more: false, total: 0, queue: QUEUE,
    })),
  },
}))

describe('an edited queue entry must not adopt pre-send state', () => {
  beforeEach(() => {
    preSendStash.clear()
    queuedSendStash.clear()
    QUEUE = []
  })

  const store = () => configureStore({ reducer: { chat: chatReducer } })

  it('refuses adoption when the fetched entry is marked edited', async () => {
    stashPreSend('s-edit', { raw: 'the ORIGINAL text', files: ['/a.png'], sent: 'the ORIGINAL text' })
    QUEUE = [{ id: 'q-edit', content: 'the EDITED text', sendId: 's-edit', edited: true }]

    await store().dispatch(warmSlotCache({ key: 'slot-e' }) as never)

    expect(queuedSendStash.get('q-edit'),
      'adopting would let Cancel restore the pre-edit text and files')
      .toBeUndefined()
  })

  it('still adopts an UNEDITED entry', async () => {
    // Positive control: a hardwired skip would satisfy the assertion above while breaking the
    // redacted-entry case this stash exists for.
    stashPreSend('s-ok', { raw: 'kept @/a.png', files: ['/a.png'], sent: 'kept @/a.png' })
    QUEUE = [{ id: 'q-ok', content: 'kept', sendId: 's-ok' }]

    await store().dispatch(warmSlotCache({ key: 'slot-o' }) as never)

    const rec = queuedSendStash.get('q-ok')
    expect(rec?.files, 'the attachments are what the parser fallback cannot recover').toEqual(['/a.png'])
    expect(rec?.sent, 'the fetched content settles the cancel guard').toBe('kept')
  })
})
