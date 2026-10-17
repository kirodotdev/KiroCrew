import { describe, it, expect } from 'vitest'
import reducer, { appendMessage, endLocalTurn, selectContinuable, sseChatMessage, startLocalTurn, syncSlotRunningFromServer } from '../store/chatSlice'
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
 *
 * The same stale page carries `running: false` -- the slot as it was before
 * the send -- and taking that verdict idled the slot under the turn the send
 * opened. A user row last on an idle slot is what `selectContinuable` offers
 * Resume on, so the composer swapped its "Message …" placeholder for the
 * interrupted-turn one until the reply's first chunk. The run state the send
 * established must survive the page too.
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

/** `selectContinuable` reads the dashboard's slot list for the subagent and
 *  remote-binding gates; this slot has neither, so an empty list stands in. */
const continuable = (chat: ReturnType<typeof init>) =>
  selectContinuable({ chat, dashboard: { slots: [] } } as never)

describe('switchSlot.fulfilled leaves the run state of a send that landed while the page fetch was in flight', () => {
  it('keeps the slot running when an idle page predates the send', () => {
    let s = openSlot()
    s = reducer(s, appendMessage(user('Explain the next step for this project.', { sendId: 's-10' })))
    s = reducer(s, startLocalTurn(SLOT))
    expect(s.slotRunning).toBe(true)
    expect(s.pendingTurnSlot).toBe(SLOT)
    expect(continuable(s)).toBe(false)

    // The page the switch asked for, read before the send: empty and idle.
    s = pageLands(s, [])
    expect(s.messages.map(m => m.content)).toEqual(['Explain the next step for this project.'])
    expect(s.slotRunning).toBe(true)
    expect(s.pendingTurnSlot).toBe(SLOT)
    // No Resume offer: the slot is still busy with the turn just sent.
    expect(continuable(s)).toBe(false)
  })

  it('keeps the slot running after a live slots frame confirmed the turn before the page landed', () => {
    let s = openSlot()
    s = reducer(s, appendMessage(user('hello', { sendId: 's-11' })))
    s = reducer(s, startLocalTurn(SLOT))
    // The server's own `slots` frame: the turn is running. It retires the
    // pending mark, so the stale page below can no longer hide behind it.
    s = reducer(s, syncSlotRunningFromServer({ slot: SLOT, running: true, stopping: false }))
    expect(s.slotRunning).toBe(true)
    expect(s.pendingTurnSlot).toBeNull()

    s = pageLands(s, [])
    expect(s.slotRunning).toBe(true)
    expect(s.slotStopping).toBe(false)
    expect(continuable(s)).toBe(false)
  })

  it('keeps the chunk floor a reply raised during the fetch', () => {
    let s = openSlot()
    s = reducer(s, appendMessage(user('ping', { sendId: 's-12' })))
    s = reducer(s, startLocalTurn(SLOT))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'pong', seq: 3 }))
    expect(s.lastChunkSeq).toBe(3)
    expect(s.slotState).toBe('streaming')

    s = pageLands(s, [])
    expect(s.lastChunkSeq).toBe(3)
    expect(s.slotState).toBe('streaming')
    expect(s.slotRunning).toBe(true)
  })

  it('does not revive a turn the send itself failed to open', () => {
    let s = openSlot()
    s = reducer(s, appendMessage(user('hello', { sendId: 's-13' })))
    s = reducer(s, startLocalTurn(SLOT))
    // The POST came back refused before the page landed: nothing is running.
    s = reducer(s, endLocalTurn(SLOT))
    expect(s.slotRunning).toBe(false)
    expect(s.pendingTurnSlot).toBeNull()

    s = pageLands(s, [])
    // Kept, not forced: the local verdict (idle) stands, the page adds nothing.
    expect(s.slotRunning).toBe(false)
    expect(s.pendingTurnSlot).toBeNull()
  })

  it('takes the run verdict of a page that caught the send', () => {
    let s = openSlot()
    s = reducer(s, appendMessage(user('hello', { sendId: 's-14' })))
    s = reducer(s, startLocalTurn(SLOT))
    // Read after the send: the page carries the row and the finished reply,
    // so its `running: false` is current.
    s = pageLands(s, [
      user('hello', { sendId: 's-14', mid: 'm-14', human: true }, '2026-10-09T13:40:01.000Z'),
      { role: 'assistant', content: 'done', cls: '', ts: '2026-10-09T13:40:02.000Z', meta: { mid: 'm-14-a' } } as ChatMessage,
    ])
    expect(s.messages.map(m => m.content)).toEqual(['hello', 'done'])
    expect(s.slotRunning).toBe(false)
    expect(s.pendingTurnSlot).toBeNull()
    expect(s.slotState).toBe('idle')
  })

  it('takes a running page as before, live send or not', () => {
    let s = openSlot()
    s = reducer(s, appendMessage(user('hello', { sendId: 's-15' })))
    s = reducer(s, startLocalTurn(SLOT))
    s = pageLands(s, [], { running: true, stopping: true })
    expect(s.slotRunning).toBe(true)
    expect(s.slotStopping).toBe(true)
    expect(s.slotState).toBe('streaming')
    expect(s.pendingTurnSlot).toBeNull()
  })
})
