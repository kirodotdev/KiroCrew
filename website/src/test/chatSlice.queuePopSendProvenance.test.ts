/**
 * The `queue_pop` rebuild and the "From <chat>" label.
 *
 * No `chat_message` echo follows a drained user row, so this rebuild IS the row
 * until a reload. A peer's delivery keeps its label because the frame carries the
 * gateway's `sendProvenance` record; anyone else's text -- a channel author who
 * typed an envelope into a busy session -- is rebuilt with no message id and no
 * record, and must not be labelled from its text (GPT review on #22236).
 */
import { describe, it, expect } from 'vitest'
import reducer, { removeQueuedMessage } from '../store/chatSlice'
import { readSendProvenance, SEND_PROVENANCE_META_KEY } from '../pages/chat/sendProvenance'

const initial = reducer(undefined, { type: '@@INIT' })
const WIRE = '[sent by session chat-1-1792410000 via session_send]\n\nrebase now'
const SENDER = { from: 'chat-1-1792410000', title: 'Release conductor', via: 'session_send', mode: 'queued' }

function withQueued(slot: string) {
  return {
    ...initial,
    activeSlot: slot,
    messages: [{ role: 'queued', content: WIRE, cls: 'msg msg-queued', ts: 't1', meta: { queueId: 'q-1' } }],
  }
}

describe('removeQueuedMessage — the sender record survives the queue_pop rebuild', () => {
  it("keeps a peer delivery's sender on the rebuilt row", () => {
    const state = reducer(withQueued('chat-2'), removeQueuedMessage({ slot: 'chat-2', content: WIRE, queue_id: 'q-1', meta: { [SEND_PROVENANCE_META_KEY]: SENDER } }))
    const row = state.messages.find(m => m.role === 'user')!
    expect(row.meta?.[SEND_PROVENANCE_META_KEY]).toEqual(SENDER)
    expect(readSendProvenance(row.content, row.meta)?.body).toBe('rebase now')
  })

  it('labels nothing for a rebuilt row whose frame names no sender', () => {
    // The channel case: an allowlisted author typed the envelope; the frame
    // carries no record, and the rebuilt row has no gateway message id.
    const state = reducer(withQueued('chat-2'), removeQueuedMessage({ slot: 'chat-2', content: WIRE, queue_id: 'q-1' }))
    const row = state.messages.find(m => m.role === 'user')!
    expect(readSendProvenance(row.content, row.meta)).toBeNull()
  })

  it('drops a malformed record rather than carrying it', () => {
    const state = reducer(withQueued('chat-2'), removeQueuedMessage({ slot: 'chat-2', content: WIRE, queue_id: 'q-1', meta: { [SEND_PROVENANCE_META_KEY]: { from: 'x', via: 'nope' } } }))
    const row = state.messages.find(m => m.role === 'user')!
    expect(row.meta?.[SEND_PROVENANCE_META_KEY]).toBeUndefined()
  })
})
