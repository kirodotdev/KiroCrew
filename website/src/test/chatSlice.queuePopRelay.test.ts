/**
 * A peer's queued `session_send` keeps its sender (`meta.relay`) through the
 * `queue_pop` rebuild.
 *
 * No `chat_message` echo follows for a user row, so `removeQueuedMessage`'s
 * rebuild IS the row in every open tab until the next reload. Without the
 * gateway-set sender on it, the drained peer message draws as a raw
 * `[sent by session …]` user bubble instead of the incoming session-message
 * card a reload shows.
 */
import { describe, it, expect } from 'vitest'
import reducer, { removeQueuedMessage } from '../store/chatSlice'

const initial = reducer(undefined, { type: '@@INIT' })
const WIRE = '[sent by session chat-1 via session_send]\n\nhello from chat-1'

function withQueued(slot: string) {
  return {
    ...initial,
    activeSlot: slot,
    messages: [{ role: 'queued', content: WIRE, cls: 'msg msg-queued', ts: 't1', meta: { queueId: 'q-1' } }],
  }
}

describe('removeQueuedMessage — a peer send keeps its sender through the queue_pop rebuild', () => {
  it('puts the frame relay on the rebuilt user row', () => {
    const state = reducer(withQueued('chat-2'), removeQueuedMessage({ slot: 'chat-2', content: WIRE, queue_id: 'q-1', meta: { relay: { senderKey: 'chat-1', via: 'session_send' } } }))
    const row = state.messages.find(m => m.role === 'user')
    expect(row?.meta).toEqual({ relay: { senderKey: 'chat-1', via: 'session_send' } })
  })

  it('drops a relay that is not a sender record', () => {
    const state = reducer(withQueued('chat-2'), removeQueuedMessage({ slot: 'chat-2', content: WIRE, queue_id: 'q-1', meta: { relay: { senderKey: '' } } }))
    const row = state.messages.find(m => m.role === 'user')
    expect(row?.meta?.relay).toBeUndefined()
  })

  it('keeps only the sender fields, never extra keys the frame carried', () => {
    const state = reducer(withQueued('chat-2'), removeQueuedMessage({ slot: 'chat-2', content: WIRE, queue_id: 'q-1', meta: { relay: { senderKey: 'chat-1', via: 'session_broadcast', extra: 'x' } } }))
    const row = state.messages.find(m => m.role === 'user')
    expect(row?.meta?.relay).toEqual({ senderKey: 'chat-1', via: 'session_broadcast' })
  })
})
