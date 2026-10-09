import { describe, it, expect } from 'vitest'
import reducer, { appendMessage, sseChatMessage } from '../store/chatSlice'
import type { ChatMessage } from '../types'
import './mockApiClient'

/**
 * A send into the slot being opened. The deep link `/chat?sid=` dispatches
 * `switchSlot` (pending: composer live, page fetch in flight); the user sends
 * before that page lands. The page was read BEFORE the send, so it does not
 * carry the bubble -- replacing the view with it wholesale would drop the
 * user's own message until the end-of-turn refresh.
 *
 * `switchSlot.fulfilled` must keep the sends that landed past the `pending`
 * baseline, whether they are still optimistic or already echoed, and must not
 * double one the page did catch.
 */

const SLOT = 'deep-linked-slot'
const init = () => reducer(undefined, { type: '@@INIT' })

const user = (content: string, meta: Record<string, unknown>, ts = '2026-10-09T13:40:00.000Z'): ChatMessage =>
  ({ role: 'user', content, cls: '', ts, meta })

function openSlot(state = init(), requestId = 'r1') {
  return reducer(state, {
    type: 'chat/switchSlot/pending',
    meta: { arg: SLOT, requestId, requestStatus: 'pending' },
  })
}

function pageLands(state: ReturnType<typeof init>, messages: ChatMessage[], extra: Record<string, unknown> = {}, requestId = 'r1') {
  return reducer(state, {
    type: 'chat/switchSlot/fulfilled',
    meta: { arg: SLOT, requestId, requestStatus: 'fulfilled' },
    payload: { key: SLOT, messages, running: false, hasMore: false, queue: [], ...extra },
  })
}

describe('switchSlot.fulfilled keeps a send that landed while the page fetch was in flight', () => {
  it('keeps the still-optimistic bubble when the page predates the send', () => {
    let s = openSlot()
    expect(s.activeSlot).toBe(SLOT)
    expect(s.messages).toEqual([])
    s = reducer(s, appendMessage(user('Explain the next step for this project.', { sendId: 's-1' })))
    expect(s.messages).toHaveLength(1)

    // The page the switch asked for, read before the send: empty.
    s = pageLands(s, [])
    expect(s.messages.map(m => [m.role, m.content])).toEqual([
      ['user', 'Explain the next step for this project.'],
    ])
    expect(s.messages[0].meta?.sendId).toBe('s-1')
    expect(s.slotLoading).toBe(false)
  })

  it('keeps the bubble after its echo gave it the server mid', () => {
    let s = openSlot()
    s = reducer(s, appendMessage(user('hello', { sendId: 's-2' })))
    // The server echo lands first (it is fast); the stale page lands after.
    s = reducer(s, sseChatMessage({
      slot: SLOT, role: 'user', content: 'hello', ts: '2026-10-09T13:40:01.000Z',
      meta: { sendId: 's-2', mid: 'm-server', human: true },
    }))
    expect(s.messages).toHaveLength(1)
    expect(s.messages[0].meta?.mid).toBe('m-server')
    expect(s.messages[0].meta?.optimistic).toBeUndefined()

    s = pageLands(s, [])
    expect(s.messages.map(m => m.content)).toEqual(['hello'])
    expect(s.messages[0].meta?.mid).toBe('m-server')
  })

  it('does not double a send the page already caught', () => {
    let s = openSlot()
    s = reducer(s, appendMessage(user('hello', { sendId: 's-3' })))
    // The page was read after the send persisted: it carries the row, with
    // the client meta the server stores opaquely plus its own mid.
    s = pageLands(s, [user('hello', { sendId: 's-3', mid: 'm-3', human: true }, '2026-10-09T13:40:01.000Z')])
    expect(s.messages.map(m => m.content)).toEqual(['hello'])
    expect(s.messages[0].meta?.mid).toBe('m-3')
  })

  it('keeps the send in front of a reply that streamed in during the fetch', () => {
    let s = openSlot()
    s = reducer(s, appendMessage(user('ping', { sendId: 's-4' })))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'pong', seq: 1 }))
    expect(s.messages.map(m => m.role)).toEqual(['user', 'streaming'])

    s = pageLands(s, [])
    expect(s.messages.map(m => [m.role, m.content])).toEqual([
      ['user', 'ping'],
      ['streaming', 'pong'],
    ])
  })

  it('leaves a cached row the page no longer carries out of the rescue', () => {
    // A slot opened before, parked with a cached transcript whose user row has
    // a sendId (it was sent from this tab). The server since truncated it.
    const cached = [user('old send', { sendId: 's-old', mid: 'm-old' }), { role: 'assistant', content: 'old reply', cls: '', ts: '2026-10-09T13:00:01.000Z', meta: { mid: 'm-old-a' } } as ChatMessage]
    let s = { ...init(), slotMessages: { [SLOT]: cached } }
    s = openSlot(s)
    expect(s.messages).toHaveLength(2) // pending restored the cache
    s = pageLands(s, [])
    // Not a live send: the cache is the pre-fetch baseline, not something the
    // user did while the page was in flight.
    expect(s.messages.filter(m => m.content === 'old send')).toHaveLength(0)
  })
})
