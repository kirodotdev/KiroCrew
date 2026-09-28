/**
 * A queued send's rebuilt user row keeps the attachment lists the `queue_pop`
 * frame carries.
 *
 * No `chat_message` echo follows for a user row, so `removeQueuedMessage`'s
 * rebuild IS the row until the next reload. It used to carry no meta at all:
 * the renderer then resolved the row's `[attached_file N] path` marker by a
 * whitespace-bounded capture and a spaced path (`/tmp/My Report.pdf`) came back
 * as `/tmp/My` -- an attachment card that opened nothing (GPT finding on
 * #9436). The drain now stamps the entry's `files` / `dirs` onto the frame and
 * the rebuild keeps them, so the marker resolves losslessly against the list.
 */
import { describe, it, expect } from 'vitest'
import reducer, { removeQueuedMessage } from '../store/chatSlice'

const initial = reducer(undefined, { type: '@@INIT' })
const WIRE = 'summarize this\n[attached_file 1] /tmp/My Report.pdf'

function withQueued(slot: string) {
  return {
    ...initial,
    activeSlot: slot,
    messages: [{ role: 'queued', content: WIRE, cls: 'msg msg-queued', ts: 't1', meta: { queueId: 'q-1' } }],
  }
}

describe('removeQueuedMessage — attachment lists survive the queue_pop rebuild', () => {
  it('puts the frame meta on the rebuilt user row', () => {
    const state = reducer(withQueued('chat-1'), removeQueuedMessage({ slot: 'chat-1', content: WIRE, queue_id: 'q-1', meta: { files: ['/tmp/My Report.pdf'] } }))
    const row = state.messages.find(m => m.role === 'user')
    expect(row).toBeDefined()
    expect(row?.content).toBe(WIRE)
    // The list is what `parseFiles` reads FIRST; with it present the marker
    // resolves to the full spaced path instead of the `\S+` fallback's `/tmp/My`.
    expect(row?.meta).toEqual({ files: ['/tmp/My Report.pdf'] })
    expect(state.messages.some(m => m.role === 'queued')).toBe(false)
  })

  it('carries folder lists the same way', () => {
    const wire = 'check [attached_dir 1] /home/u/my designs/'
    const st = { ...withQueued('chat-1'), messages: [{ role: 'queued', content: wire, cls: 'msg msg-queued', ts: 't1', meta: { queueId: 'q-1' } }] }
    const state = reducer(st, removeQueuedMessage({ slot: 'chat-1', content: wire, queue_id: 'q-1', meta: { dirs: ['/home/u/my designs/'] } }))
    expect(state.messages.find(m => m.role === 'user')?.meta).toEqual({ dirs: ['/home/u/my designs/'] })
  })

  it('a frame without meta rebuilds the prior row shape (no meta key)', () => {
    // Pinned as an ABSENT key: an empty `meta: {}` would be a shape change every
    // meta-keyed consumer (mid lookups, pin controls) would have to tolerate.
    const state = reducer(withQueued('chat-1'), removeQueuedMessage({ slot: 'chat-1', content: WIRE, queue_id: 'q-1' }))
    const row = state.messages.find(m => m.role === 'user')
    expect(row).toBeDefined()
    expect('meta' in (row as object)).toBe(false)
  })

  it('an empty frame meta is treated as absent', () => {
    const state = reducer(withQueued('chat-1'), removeQueuedMessage({ slot: 'chat-1', content: WIRE, queue_id: 'q-1', meta: {} }))
    expect('meta' in (state.messages.find(m => m.role === 'user') as object)).toBe(false)
  })

  it('rebuilds into a cached non-active slot too', () => {
    const st = { ...initial, activeSlot: 'chat-other', slotMessages: { 'chat-1': [{ role: 'queued', content: WIRE, cls: 'msg msg-queued', ts: 't1', meta: { queueId: 'q-1' } }] } }
    const state = reducer(st, removeQueuedMessage({ slot: 'chat-1', content: WIRE, queue_id: 'q-1', meta: { files: ['/tmp/My Report.pdf'] } }))
    expect(state.slotMessages['chat-1'].find(m => m.role === 'user')?.meta).toEqual({ files: ['/tmp/My Report.pdf'] })
  })
})
